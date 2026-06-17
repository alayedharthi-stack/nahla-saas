"""Product Media Identity Guard — OCR + vision + catalog matching."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.product_media_identity_guard import (  # noqa: E402
    MSG_CONFIDENT_AR,
    MSG_NO_MATCH_AR,
    MSG_UNREADABLE_AR,
    MSG_WEAK_AR,
    MediaIdentityEvidence,
    classify_media_catalog_match,
    collect_media_identity_evidence,
    evaluate_product_media_identity,
    try_product_media_identity_decision,
)
from modules.ai.knowledge.product_matcher import CatalogProductForMatch  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PRODUCT_MEDIA_IDENTITY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.intent.conversation_objective_guard import (  # noqa: E402
    OBJECTIVE_PRODUCT_ORIGIN,
    is_product_ownership_question,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402

_FORBIDDEN_TERMS = (
    "وكيل",
    "موزع",
    "مورد",
    "distributor",
    "supplier",
    "مصر",
    "egypt",
    "بلد المنشأ",
)


def _image_profile(*, ocr: str = "", vision: str = "") -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "normalized_type": "image",
        "has_image": True,
    }
    if ocr:
        meta["ocr_text"] = ocr
    if vision:
        meta["frame_vision_text"] = vision
    return {"inbound_metadata": meta}


def _ctx(
    message: str,
    *,
    profile: Optional[Dict[str, Any]] = None,
    products: Optional[List[CatalogProductForMatch]] = None,
    state: Optional[MerchantConversationState] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message=message,
        profile=profile or {},
        history=[],
        state=state or MerchantConversationState(turn=1),
        tenant_id=42,
        merchant_context={"products": []},
        _db=None,
    )


def _catalog(*titles: str) -> List[CatalogProductForMatch]:
    return [
        CatalogProductForMatch(
            id=i + 1,
            title=title,
            sku=f"SKU-{i + 1}",
            external_id=f"ext-{i + 1}",
        )
        for i, title in enumerate(titles)
    ]


class TestOwnershipDetection:
    @pytest.mark.parametrize(
        "phrase",
        [
            "هل هذا تبعكم؟",
            "هل هذا منتجكم؟",
            "هل هذا من عندكم؟",
            "ده تبعكم؟",
            "المنتج ده تبعكم؟",
            "تابع لكم؟",
            "is this yours?",
            "is this your product?",
        ],
    )
    def test_ownership_phrases_detected(self, phrase: str) -> None:
        assert is_product_ownership_question(phrase)


class TestMediaIdentityVerdicts:
    def test_confident_match_with_image_and_ownership(self) -> None:
        title = "زيت الزيتون البكر الممتاز"
        ctx = _ctx(
            "هل هذا تبعكم؟",
            profile=_image_profile(ocr=title, vision="عبوة زجاجية بملصق أخضر"),
        )
        catalog = _catalog(title)

        with patch(
            "modules.ai.brain.commerce.product_media_identity_guard.resolve_catalog_for_match",
            return_value=catalog,
        ):
            decision = try_product_media_identity_decision(ctx)

        assert decision is not None
        assert decision.action == ACTION_PRODUCT_MEDIA_IDENTITY
        assert title in decision.args["reply_text"]
        assert "بحسب المنتجات المتزامنة حالياً" in decision.args["reply_text"]
        assert decision.args["block_purchase_flow"] is True
        assert decision.args["media_identity_status"] == "confident"

    def test_weak_match(self) -> None:
        title = "شامبو الأعشاب الطبيعي"
        ctx = _ctx(
            "هل هذا منتجكم؟",
            profile=_image_profile(
                ocr="شامبو الأعشاب الطبيعي جزئي",
                vision="زجاجة بلاستيك شفافة",
            ),
        )
        catalog = _catalog(title, "شامبو الأعشاب المركز", "بلسم الأعشاب")

        with patch(
            "modules.ai.brain.commerce.product_media_identity_guard.resolve_catalog_for_match",
            return_value=catalog,
        ):
            verdict = evaluate_product_media_identity(ctx)

        assert verdict.status in {"weak", "confident"}
        assert verdict.reply_text in {MSG_WEAK_AR, MSG_CONFIDENT_AR.format(product_name=title)}

    def test_no_match(self) -> None:
        ctx = _ctx(
            "هل هذا من عندكم؟",
            profile=_image_profile(
                ocr="منتج غريب جداً لا يشبه الكتالوج",
                vision="صندوق أحمر بدون علامة",
            ),
        )
        catalog = _catalog("زيت الزيتون", "عسل سدر")

        with patch(
            "modules.ai.brain.commerce.product_media_identity_guard.resolve_catalog_for_match",
            return_value=catalog,
        ):
            verdict = evaluate_product_media_identity(ctx)

        assert verdict.status == "no_match"
        assert verdict.reply_text == MSG_NO_MATCH_AR

    def test_unreadable_image(self) -> None:
        ctx = _ctx(
            "هل هذا تبعكم؟",
            profile=_image_profile(ocr="", vision=""),
        )

        verdict = evaluate_product_media_identity(ctx)
        assert verdict.status == "unreadable"
        assert verdict.reply_text == MSG_UNREADABLE_AR

    def test_ownership_without_image_skipped(self) -> None:
        ctx = _ctx("هل هذا تبعكم؟", profile={})
        assert try_product_media_identity_decision(ctx) is None


class TestObjectiveInheritance:
    def test_stamps_objective_evidence_after_image(self) -> None:
        title = "كريم مرطب بالألوفera"
        state = MerchantConversationState(turn=3)
        state.active_conversation_objective = OBJECTIVE_PRODUCT_ORIGIN
        ctx = _ctx(
            "هل هذا منتجكم؟",
            profile=_image_profile(ocr=title),
            state=state,
        )
        catalog = _catalog(title)

        with patch(
            "modules.ai.brain.commerce.product_media_identity_guard.resolve_catalog_for_match",
            return_value=catalog,
        ):
            try_product_media_identity_decision(ctx)

        assert state.objective_evidence.get("media_ocr_preview")
        assert state.objective_evidence.get("catalog_match_status") == "confident"
        assert state.objective_evidence.get("catalog_match_product_id") == 1

    def test_history_vision_used_when_follow_up_has_no_image(self) -> None:
        title = "لوشن الجسم المرطب"
        state = MerchantConversationState(turn=5)
        history = [
            {
                "direction": "in",
                "body": f"[وصف الصورة المرسلة] {title} على رف",
            },
        ]
        ctx = SimpleNamespace(
            message="is this your product?",
            profile={},
            history=history,
            state=state,
            tenant_id=42,
            merchant_context={"products": []},
            _db=None,
        )
        catalog = _catalog(title)

        with patch(
            "modules.ai.brain.commerce.product_media_identity_guard.resolve_catalog_for_match",
            return_value=catalog,
        ):
            decision = try_product_media_identity_decision(ctx)

        assert decision is not None
        assert decision.action == ACTION_PRODUCT_MEDIA_IDENTITY


class TestForbiddenClaims:
    @pytest.mark.parametrize("term", _FORBIDDEN_TERMS)
    def test_replies_never_contain_supply_chain_terms(self, term: str) -> None:
        for msg in (MSG_CONFIDENT_AR, MSG_WEAK_AR, MSG_NO_MATCH_AR, MSG_UNREADABLE_AR):
            assert term.lower() not in msg.lower()

    def test_classify_never_invents_distributor_country_supplier(self) -> None:
        evidence = MediaIdentityEvidence(
            ocr_text="زيت زيتون بكر ممتاز من المتجر",
            vision_text="عبوة زجاجية",
            combined_text="زيت زيتون بكر ممتاز من المتجر عبوة زجاجية",
            readable=True,
            has_current_image=True,
            source="current_image",
        )
        catalog = _catalog("زيت زيتون بكر")
        verdict = classify_media_catalog_match(evidence, catalog)
        combined = f"{verdict.reply_text} {verdict.matched_product_title}"
        for term in _FORBIDDEN_TERMS:
            assert term.lower() not in combined.lower()


class TestPurchaseFlowBlocked:
    def test_decision_blocks_purchase_flow(self) -> None:
        title = "صابون طبيعي باللافender"
        ctx = _ctx(
            "هل هذا تبعكم؟",
            profile=_image_profile(ocr=title),
        )
        catalog = _catalog(title)

        with patch(
            "modules.ai.brain.commerce.product_media_identity_guard.resolve_catalog_for_match",
            return_value=catalog,
        ):
            decision = try_product_media_identity_decision(ctx)

        assert decision is not None
        assert decision.args.get("block_purchase_flow") is True
        assert decision.args.get("block_commerce_escalation") is True
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


class TestEvidenceCollection:
    def test_collects_ocr_and_vision_from_profile(self) -> None:
        evidence = collect_media_identity_evidence(
            message="هل هذا تبعكم؟",
            profile=_image_profile(
                ocr="اسم المنتج على الملصق",
                vision="شعار دائري أخضر",
            ),
        )
        assert "اسم المنتج" in evidence.ocr_text
        assert "شعار" in evidence.vision_text
        assert evidence.readable is True
        assert evidence.has_current_image is True
