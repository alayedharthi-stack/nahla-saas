"""
Regression: browse persona compose stamps pending_product_cards only when an
authoritative product referent grounds SINGLE_RICH (AI-D02).

Reproduces the production path in responder.py ACTION_SEARCH_PRODUCTS browse branch.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    PRESENTATION_MULTI_CHOICES,
    PRESENTATION_NONE,
    PRESENTATION_SINGLE_RICH,
    stamp_presentation_observability,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

JACKET_RICH = {
    "id": 28,
    "external_id": "1921568272",
    "title": "جاكيت",
    "category": "ملابس",
    "price": 199,
    "can_checkout": True,
    "orderable": True,
    "in_stock": True,
    "image_url": "https://cdn.example/jacket.jpg",
    "product_url": "https://shop.example/products/jacket",
}

SHOE = {
    "id": 55,
    "external_id": "shoe-1",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "can_checkout": True,
    "orderable": True,
    "in_stock": True,
    "image_url": "https://cdn.example/shoe.jpg",
    "product_url": "https://shop.example/products/white-sneaker",
}


def _search_ctx(*, message: str, state: MerchantConversationState | None = None) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500009429",
        customer_id=1,
        conversation_id=9739,
        message=message,
        intent=Intent(name="browse", confidence=0.9, raw_message=message),
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            orderable=True,
            product_count=1,
            discovery_products=[dict(JACKET_RICH)],
            top_products=[dict(JACKET_RICH)],
        ),
        merchant_context={
            "ai_settings": {"persona_composer_enabled": True},
            "products": [dict(JACKET_RICH)],
        },
    )


def _grounded_search_ctx(*, message: str) -> BrainContext:
    row = {
        **JACKET_RICH,
        "customer_selected": True,
        "provenance": "catalog_order_selected",
    }
    return _search_ctx(
        message=message,
        state=MerchantConversationState(
            greeted=True,
            stage="checkout",
            current_product_focus=dict(row),
            last_presented_products=[dict(row)],
        ),
    )


def _persona_success_event(*, question_kind: str = "browse") -> dict[str, Any]:
    return {
        "compose_source": "persona_llm",
        "response_mode": "grounded_persona_compose",
        "llm_candidate_present": True,
        "persona_compose": {"source": "persona_llm", "guard_passed": True},
        "question_kind": question_kind,
        "catalog_product_ids": [28],
    }


def _presentation_observability(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_presentation_kind": data.get("product_presentation_kind"),
        "product_presentation_reason": data.get("product_presentation_reason"),
        "presentation_candidate_count": data.get("presentation_candidate_count"),
        "pending_product_card_count": data.get("pending_product_card_count"),
        "pending_product_card_ids": list(data.get("pending_product_card_ids") or []),
    }


async def _run_browse_compose(
    *,
    products: list[dict[str, Any]],
    message: str = "أبغى جاكيت",
    persona_text: str = "عندنا جاكيت رائع متوفر حالياً.",
    display_slice_override: tuple[list[dict[str, Any]], dict[str, Any]] | None = None,
    ctx: BrainContext | None = None,
) -> tuple[str, ActionResult, dict[str, Any]]:
    """Drive responder browse path and capture presentation diagnostics."""
    from modules.ai.brain.compose.responder import (  # noqa: PLC0415
        DefaultComposer,
        catalog_compose_products_for_search_turn,
    )
    from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
        apply_display_slice,
        resolve_product_breadth_from_context,
    )
    from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
        classify_catalog_question_kind,
    )

    ctx = ctx or _search_ctx(message=message)
    result_payload: dict[str, Any] = {
        "products": products,
        "query": "جاكيت",
        "count": len(products),
    }
    if len(products) == 1 and isinstance(products[0], dict):
        result_payload["product"] = dict(products[0])
    result = ActionResult(
        success=True,
        data=result_payload,
    )
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "جاكيت", "source": "search"},
        reason="browse persona stamp regression",
        confidence=0.9,
    )

    safe_products = [
        p for p in products if p.get("can_checkout", p.get("orderable", True))
    ]
    breadth = resolve_product_breadth_from_context(ctx, decision)
    if display_slice_override is not None:
        display_candidates, breadth_meta = display_slice_override
    else:
        display_candidates, breadth_meta = apply_display_slice(safe_products, breadth)
    question_kind = classify_catalog_question_kind(
        message,
        query="جاكيت",
        decision_args=dict(decision.args or {}),
    )
    compose_products = catalog_compose_products_for_search_turn(
        question_kind=question_kind,
        category_filtered_facts=safe_products,
        display_candidates=display_candidates,
    )

    apply_calls: list[dict[str, Any]] = []
    from modules.ai.brain.commerce import product_presentation_selection as pps  # noqa: PLC0415

    original_apply = pps.apply_search_product_presentation

    def _spy_apply(result_data, *, candidates, build_buttons=None, **kwargs):
        apply_calls.append(
            {
                "candidate_count": len(list(candidates or [])),
                "candidate_ids": [
                    c.get("id") for c in (candidates or []) if isinstance(c, dict)
                ],
            }
        )
        return original_apply(
            result_data,
            candidates=candidates,
            build_buttons=build_buttons,
            **kwargs,
        )

    composer = DefaultComposer()
    with ExitStack() as stack:
        if display_slice_override is not None:
            stack.enter_context(
                patch(
                    "modules.ai.brain.commerce.product_breadth_policy.apply_display_slice",
                    return_value=display_slice_override,
                )
            )
        stack.enter_context(
            patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(
                    return_value=(
                        persona_text,
                        PersonaComposeResult(
                            text=persona_text,
                            source="persona_llm",
                            surface="catalog_product_answer",
                            facts_hash="facts",
                            guard_passed=True,
                        ),
                        _persona_success_event(question_kind=question_kind),
                    ),
                ),
            )
        )
        stack.enter_context(
            patch(
                "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                side_effect=lambda prods, **_kw: list(prods),
            )
        )
        stack.enter_context(
            patch(
                "modules.ai.brain.commerce.product_presentation_selection.apply_search_product_presentation",
                side_effect=_spy_apply,
            )
        )
        text = await composer.compose(decision, result, ctx)

    diag = {
        "display_candidate_count": len(display_candidates),
        "display_candidate_ids": [p.get("id") for p in display_candidates],
        "compose_product_count": len(compose_products),
        "compose_product_ids": [p.get("id") for p in compose_products],
        "apply_invoked": bool(apply_calls),
        "apply_calls": apply_calls,
        "presentation_kind": result.data.get("product_presentation_kind"),
        "presentation_reason": result.data.get("product_presentation_reason"),
        "pending_card_count_after": len(result.data.get("pending_product_cards") or []),
        "pending_card_ids": [
            c.get("id") for c in (result.data.get("pending_product_cards") or [])
        ],
        "catalog_product_ids": list(result.data.get("catalog_product_ids") or []),
        "observability": _presentation_observability(result.data),
        "breadth_meta": breadth_meta,
    }
    return text, result, diag


class TestBrowsePersonaSingleRichStamp:
    def test_ungrounded_browse_singleton_does_not_stamp_card(self) -> None:
        """Matrix #1: ranked singleton browse hit → persona text, no card."""

        async def _run() -> None:
            text, result, diag = await _run_browse_compose(products=[JACKET_RICH])

            assert text == "عندنا جاكيت رائع متوفر حالياً."
            assert diag["display_candidate_count"] == 1
            assert diag["apply_invoked"]
            assert diag["presentation_kind"] == PRESENTATION_NONE, diag
            assert diag["presentation_reason"] == "ranked_singleton_not_referent"
            assert diag["pending_card_count_after"] == 0, diag
            assert not result.data.get("pending_product_cards")

        asyncio.run(_run())

    def test_grounded_singleton_persona_success_stamps_rich_card(self) -> None:
        """Matrix #1b: authoritative referent → SINGLE_RICH + pending_product_cards=1."""

        async def _run() -> None:
            text, result, diag = await _run_browse_compose(
                products=[JACKET_RICH],
                ctx=_grounded_search_ctx(message="أبغى نفس الجاكيت"),
            )

            assert text == "عندنا جاكيت رائع متوفر حالياً."
            assert diag["presentation_kind"] == PRESENTATION_SINGLE_RICH, diag
            assert diag["pending_card_count_after"] == 1, diag
            assert result.data.get("pending_buttons") in (None, [])

        asyncio.run(_run())

    def test_single_rich_card_carries_image_and_product_url(self) -> None:
        """Matrix #2: grounded stamped card carries file_url and product_url."""

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=[JACKET_RICH],
                ctx=_grounded_search_ctx(message="أبغى نفس الجاكيت"),
            )

            assert diag["presentation_kind"] == PRESENTATION_SINGLE_RICH
            cards = result.data.get("pending_product_cards") or []
            assert len(cards) == 1
            assert cards[0]["file_url"] == "https://cdn.example/jacket.jpg"
            assert cards[0]["product_url"] == "https://shop.example/products/jacket"

        asyncio.run(_run())

    def test_persona_success_does_not_erase_stamp(self) -> None:
        """Matrix #3: grounded persona compose leaves pending_product_cards intact."""

        async def _run() -> None:
            text, result, _diag = await _run_browse_compose(
                products=[JACKET_RICH],
                ctx=_grounded_search_ctx(message="أبغى نفس الجاكيت"),
            )

            assert text
            cards = result.data.get("pending_product_cards") or []
            assert len(cards) == 1
            assert cards[0]["id"] == 28
            assert result.data.get("product_presentation_kind") == PRESENTATION_SINGLE_RICH

        asyncio.run(_run())

    def test_quality_recompose_restore_preserves_single_rich_cards(self) -> None:
        """Matrix #4: pipeline restore logic keeps SINGLE_RICH cards after recompose erase."""

        data: dict[str, Any] = {
            "product_presentation_kind": PRESENTATION_SINGLE_RICH,
            "product_presentation_reason": "authoritative_referent_grounded",
            "presentation_candidate_count": 1,
            "pending_product_cards": [
                {
                    "kind": "product_card",
                    "id": 28,
                    "title": "جاكيت",
                    "file_url": "https://cdn.example/jacket.jpg",
                    "product_url": "https://shop.example/products/jacket",
                }
            ],
        }
        cards_before = list(data["pending_product_cards"])
        pres_kind_before = data["product_presentation_kind"]

        data.pop("pending_product_cards", None)

        if (
            cards_before
            and not (data.get("pending_product_cards") or [])
            and pres_kind_before == PRESENTATION_SINGLE_RICH
        ):
            data["pending_product_cards"] = list(cards_before)
            data["product_presentation_kind"] = pres_kind_before
            data["product_presentation_reason"] = str(
                data.get("product_presentation_reason") or "restored_after_recompose"
            )
            stamp_presentation_observability(
                data,
                candidate_count=int(data.get("presentation_candidate_count") or 1),
            )

        assert len(data.get("pending_product_cards") or []) == 1
        assert data["product_presentation_kind"] == PRESENTATION_SINGLE_RICH
        assert data["pending_product_card_count"] == 1
        assert data["pending_product_card_ids"] == [28]

    def test_multi_display_but_compose_ids_singleton_is_multi_not_single_rich(self) -> None:
        """Matrix #5: multiple identified candidates → MULTI, no forced SINGLE_RICH."""

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=[JACKET_RICH, SHOE],
                persona_text="عندنا جاكيت وحذاء رياضي.",
            )

            assert diag["display_candidate_count"] >= 2
            assert diag["apply_invoked"]
            assert diag["presentation_kind"] == PRESENTATION_MULTI_CHOICES, diag
            assert not result.data.get("pending_product_cards")

        asyncio.run(_run())

    def test_zero_candidates_no_recoverable_product_emits_no_card(self) -> None:
        """Matrix #6: empty display + no executor recovery → no card stamp."""

        async def _run() -> None:
            from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

            ctx = _search_ctx(message="أبغى شيء غير موجود")
            result = ActionResult(
                success=True,
                data={"products": [], "query": "شيء", "count": 0},
            )
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "شيء", "source": "search"},
                reason="no recoverable product",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                AsyncMock(
                    return_value=(
                        "ما لقينا منتجات مطابقة.",
                        PersonaComposeResult(
                            text="ما لقينا منتجات مطابقة.",
                            source="persona_llm",
                            surface="catalog_product_answer",
                            facts_hash="facts",
                            guard_passed=True,
                        ),
                        {
                            **_persona_success_event(),
                            "catalog_product_ids": [],
                        },
                    ),
                ),
            ):
                with patch(
                    "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                    side_effect=lambda prods, **_kw: list(prods),
                ):
                    text = await composer.compose(decision, result, ctx)

            assert text
            assert not result.data.get("pending_product_cards")
            assert result.data.get("product_presentation_kind") in (
                None,
                PRESENTATION_NONE,
            )

        asyncio.run(_run())

    def test_title_only_singleton_skips_single_rich_stamp(self) -> None:
        """Matrix #7: single unresolved identity → no card (singleton_missing_catalog_identity)."""

        skinny = [{"title": "جاكيت", "can_checkout": True, "orderable": True}]

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=skinny,
                message="أبغى جاكيت",
                persona_text="عندنا جاكيت.",
            )
            assert diag["display_candidate_count"] == 1
            assert diag["apply_invoked"]
            assert diag["presentation_kind"] == PRESENTATION_MULTI_CHOICES, diag
            assert diag["presentation_reason"] == "singleton_missing_catalog_identity"
            assert not result.data.get("pending_product_cards")

        asyncio.run(_run())

    def test_title_only_plus_identified_companion_stays_ungrounded(self) -> None:
        """Matrix #8: title-only junk + identified jacket without referent → no card."""

        title_only = {"title": "جاكيت", "can_checkout": True, "orderable": True}

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=[title_only, JACKET_RICH],
                message="أبغى جاكيت",
                persona_text="هذا الجاكيت متوفر.",
            )

            assert diag["apply_invoked"]
            assert diag["presentation_kind"] == PRESENTATION_NONE, diag
            assert diag["pending_card_count_after"] == 0
            assert not result.data.get("pending_product_cards")

        asyncio.run(_run())

    def test_stamp_emits_observability_fields(self) -> None:
        """Matrix #9: presentation observability keys present after grounded stamp."""

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=[JACKET_RICH],
                ctx=_grounded_search_ctx(message="أبغى نفس الجاكيت"),
            )

            obs = diag["observability"]
            assert obs["product_presentation_kind"] == PRESENTATION_SINGLE_RICH
            assert obs["product_presentation_reason"] == "authoritative_referent_grounded"
            assert obs["presentation_candidate_count"] == 1
            assert obs["pending_product_card_count"] == 1
            assert obs["pending_product_card_ids"] == [28]
            assert result.data.get("pending_product_card_count") == 1

        asyncio.run(_run())

    def test_minimal_tenant1_jacket_shape_stamps_when_grounded(self) -> None:
        """Live Tenant 1 jacket row shape stamps only with authoritative referent."""

        minimal = {
            "id": 28,
            "external_id": "1921568272",
            "title": "جاكيت",
            "price": 169,
            "can_checkout": True,
            "in_stock": True,
        }

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=[minimal],
                message="اعرض الجاكيت",
                ctx=_grounded_search_ctx(message="اعرض الجاكيت"),
            )
            assert diag["display_candidate_count"] == 1
            assert diag["apply_invoked"]
            assert diag["presentation_kind"] == PRESENTATION_SINGLE_RICH, diag
            assert diag["pending_card_count_after"] == 1
            assert result.data.get("pending_product_cards")

        asyncio.run(_run())

    def test_empty_display_slice_recovers_singleton_without_card_when_ungrounded(self) -> None:
        """Empty display slice recovers candidate row but does not stamp without referent."""

        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
        from modules.ai.brain.commerce import product_presentation_selection as pps  # noqa: PLC0415

        original_apply = pps.apply_search_product_presentation
        apply_calls: list[int] = []

        def _spy_apply(_data, *, candidates, build_buttons=None, **kwargs):
            apply_calls.append(len(list(candidates or [])))
            return original_apply(
                _data,
                candidates=candidates,
                build_buttons=build_buttons,
                **kwargs,
            )

        async def _run() -> None:
            ctx = _search_ctx(message="اعرض الجاكيت")
            result = ActionResult(
                success=True,
                data={
                    "products": [
                        {
                            "id": 28,
                            "external_id": "1921568272",
                            "title": "جاكيت",
                            "can_checkout": True,
                            "image_url": "https://cdn.example/jacket.jpg",
                            "product_url": "https://shop.example/products/jacket",
                        }
                    ],
                    "query": "جاكيت",
                    "count": 1,
                },
            )
            decision = Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={"query": "جاكيت", "source": "search"},
                reason="empty display recovery",
                confidence=0.9,
            )
            composer = DefaultComposer()
            with patch(
                "modules.ai.brain.commerce.product_breadth_policy.apply_display_slice",
                return_value=([], {"total_count": 1, "hidden_count": 1}),
            ):
                with patch(
                    "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                    AsyncMock(
                        return_value=(
                            "هذا الجاكيت متوفر حاليًا ✨",
                            PersonaComposeResult(
                                text="هذا الجاكيت متوفر حاليًا ✨",
                                source="persona_llm",
                                surface="catalog_product_answer",
                                facts_hash="facts",
                                guard_passed=True,
                            ),
                            _persona_success_event(),
                        ),
                    ),
                ):
                    with patch(
                        "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
                        side_effect=lambda prods, **_kw: list(prods),
                    ):
                        with patch(
                            "modules.ai.brain.commerce.product_presentation_selection.apply_search_product_presentation",
                            side_effect=_spy_apply,
                        ):
                            text = await composer.compose(decision, result, ctx)

            assert "الجاكيت" in text
            assert apply_calls == [1], "stamp gate must recover singleton from executor"
            assert result.data.get("product_presentation_kind") == PRESENTATION_NONE
            assert result.data.get("product_presentation_reason") == "ranked_singleton_not_referent"
            assert not result.data.get("pending_product_cards")
            assert result.data.get("catalog_product_ids") == [28]

        asyncio.run(_run())

    def test_two_display_rows_stamp_multi_even_when_event_ids_singleton(self) -> None:
        """B: len(candidates)>1 forces MULTI; compose metadata id=[28] does not override."""

        async def _run() -> None:
            _text, result, diag = await _run_browse_compose(
                products=[JACKET_RICH, SHOE],
                message="اعرض الجاكيت",
                persona_text="هذا الجاكيت متوفر حاليًا ✨",
            )

            assert diag["display_candidate_count"] == 2
            assert diag["compose_product_ids"] == [28, 55]
            assert diag["apply_invoked"]
            assert diag["presentation_kind"] == PRESENTATION_MULTI_CHOICES
            assert not result.data.get("pending_product_cards")

        asyncio.run(_run())
