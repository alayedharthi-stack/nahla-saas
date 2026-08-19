"""AI-D08: strip-first product claim grounding ownership and recompose loop."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    ProductClaimGroundingGuardResult,
    apply_product_claim_grounding_guard,
    resolve_product_claim_second_pass_reply,
    stamp_product_claim_guard_provenance,
)


def _evidence(**overrides: Any) -> ProductClaimGroundingEvidence:
    base = dict(
        grounded_prices=frozenset({300, 400}),
        grounded_text_corpus="",
        available_products=(
            {"id": 1, "title": "قميص قطني أزرق", "can_checkout": True},
        ),
        unavailable_products=(),
        catalog_products_this_turn=False,
        catalog_miss_this_turn=False,
        recent_catalog_miss=False,
        recent_no_synced=False,
        has_checkout_catalog=True,
        executor_product_ids=frozenset(),
        kb_section_ids=frozenset(),
    )
    base.update(overrides)
    return ProductClaimGroundingEvidence(**base)


class TestSecondPassReplyResolution:
    def test_usable_strip_wins_over_recomposed(self) -> None:
        second = ProductClaimGroundingGuardResult(
            reply="أقدر أرسل الخيارات المتوفرة.",
            action="stripped",
            replaced=True,
            stripped=True,
            scrubbed_empty=False,
        )
        resolved = resolve_product_claim_second_pass_reply(
            second_pass=second,
            recomposed_reply="recomposed text",
            compose_source="persona_llm",
        )
        assert resolved == second.reply

    def test_empty_strip_keeps_fallback_deterministic_only(self) -> None:
        second = ProductClaimGroundingGuardResult(
            reply="",
            action="stripped_empty",
            replaced=True,
            stripped=True,
            scrubbed_empty=True,
        )
        resolved = resolve_product_claim_second_pass_reply(
            second_pass=second,
            recomposed_reply="fallback line",
            compose_source="fallback_deterministic",
        )
        assert resolved == "fallback line"

    def test_empty_strip_without_fallback_keeps_empty(self) -> None:
        second = ProductClaimGroundingGuardResult(
            reply="",
            action="stripped_empty",
            replaced=True,
            stripped=True,
            scrubbed_empty=True,
        )
        resolved = resolve_product_claim_second_pass_reply(
            second_pass=second,
            recomposed_reply="still ungrounded comparison",
            compose_source="persona_llm",
        )
        assert resolved == ""


class TestRecomposeLoopProtection:
    def test_second_pass_does_not_request_recompose(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence()

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )
        first = apply_product_claim_grounding_guard(
            reply="القميص أحلى من البديل.",
            tenant_id=35,
        )
        assert first.requires_grounded_recompose is True

        recomposed = "القميص أحلى من البديل."
        second = apply_product_claim_grounding_guard(
            reply=recomposed,
            tenant_id=35,
            inbound_metadata={"product_claim_recompose_performed": True},
            allow_recompose=False,
        )
        assert second.requires_grounded_recompose is False
        assert second.stripped is True


class TestPipelineProvenanceStamp:
    def test_empty_strip_then_recompose_flags(self) -> None:
        data: Dict[str, Any] = {
            "catalog_fact_products": [{"id": 1, "title": "قميص", "price": 120}],
            "products": [{"id": 1, "title": "قميص", "price": 120}],
        }
        first = ProductClaimGroundingGuardResult(
            reply="",
            action="stripped_empty",
            replaced=True,
            reason="ungrounded_comparison",
            blocked_claims=("ungrounded_comparison",),
            stripped=True,
            scrubbed_empty=True,
            requires_grounded_recompose=True,
        )
        stamp_product_claim_guard_provenance(data, first)
        data["product_claim_recompose_requested"] = True

        recomposed = "أقدر أرسل لك الخيارات المتوفرة من الكتالوج."
        data["compose_reply_candidate"] = recomposed
        data["product_claim_recompose_performed"] = True

        second = apply_product_claim_grounding_guard(
            reply=recomposed,
            tenant_id=35,
            catalog_fact_products=data["catalog_fact_products"],
            executor_products=data["products"],
            inbound_metadata={"product_claim_recompose_performed": True},
            allow_recompose=False,
        )
        stamp_product_claim_guard_provenance(
            data,
            second,
            recompose_requested=True,
            recompose_performed=True,
        )
        assert data["product_claim_recompose_requested"] is True
        assert data["product_claim_recompose_performed"] is True
        assert data["product_claim_stripped"] is True


class TestPipelineComposeOnce:
    def test_compose_called_once_on_empty_strip(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate pipeline hook: first strip empty -> compose once -> second strip."""
        import asyncio

        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence()

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )

        composer = MagicMock()
        composer.compose = AsyncMock(
            return_value="أقدر أرسل لك الخيارات المتوفرة.",
        )

        reply = "القميص أحلى من البديل."
        meta: Dict[str, Any] = {}
        result_data: Dict[str, Any] = {
            "catalog_fact_products": [{"id": 1, "title": "قميص", "price": 120}],
            "products": [{"id": 1, "title": "قميص", "price": 120}],
        }

        first = apply_product_claim_grounding_guard(
            reply=reply,
            tenant_id=35,
            catalog_fact_products=result_data["catalog_fact_products"],
            executor_products=result_data["products"],
        )
        assert first.requires_grounded_recompose is True
        reply = first.reply
        stamp_product_claim_guard_provenance(result_data, first)

        result_data["product_claim_recompose_requested"] = True
        recomposed = asyncio.run(composer.compose(None, None, None))
        result_data["compose_reply_candidate"] = recomposed
        result_data["product_claim_recompose_performed"] = True
        meta["product_claim_recompose_performed"] = True

        second = apply_product_claim_grounding_guard(
            reply=recomposed,
            tenant_id=35,
            catalog_fact_products=result_data["catalog_fact_products"],
            executor_products=result_data["products"],
            inbound_metadata=meta,
            allow_recompose=False,
        )
        stamp_product_claim_guard_provenance(
            result_data,
            second,
            recompose_requested=True,
            recompose_performed=True,
        )
        reply = resolve_product_claim_second_pass_reply(
            second_pass=second,
            recomposed_reply=recomposed,
            compose_source=str(result_data.get("compose_source") or ""),
        )

        assert composer.compose.await_count == 1
        assert reply == recomposed
        assert result_data["product_claim_recompose_performed"] is True
