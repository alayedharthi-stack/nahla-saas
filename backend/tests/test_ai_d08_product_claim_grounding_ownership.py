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
    PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_ACTION,
    PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_REASON,
    ProductClaimGroundingGuardResult,
    apply_product_claim_grounding_guard,
    finalize_product_claim_after_authorized_recompose,
    invoke_authorized_product_claim_recompose,
    resolve_product_claim_second_pass_reply,
    should_skip_quality_recompose_after_product_claim,
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
            fallback_reason="llm_compose_failed",
        )
        assert resolved == "fallback line"

    def test_empty_strip_fallback_source_without_reason_keeps_empty(self) -> None:
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
        assert resolved == ""

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
        reply = finalize_product_claim_after_authorized_recompose(
            second_pass=second,
            recomposed_reply=recomposed,
            result_data=result_data,
        )

        assert composer.compose.await_count == 1
        assert reply == recomposed
        assert result_data["product_claim_recompose_performed"] is True
        assert result_data.get("product_claim_constitutional_fallback") is not True


class TestQualityRecomposeSkipAndExport:
    def test_quality_recompose_skipped_after_product_claim_recompose(self) -> None:
        assert should_skip_quality_recompose_after_product_claim(
            {"product_claim_recompose_performed": True},
        )
        assert should_skip_quality_recompose_after_product_claim({}) is False

    def test_compose_failed_restores_recomposed_text(self) -> None:
        second = ProductClaimGroundingGuardResult(
            reply="",
            action="stripped_empty",
            replaced=True,
            stripped=True,
            scrubbed_empty=True,
        )
        resolved = resolve_product_claim_second_pass_reply(
            second_pass=second,
            recomposed_reply="constitutional fallback line",
            compose_source="persona_llm",
            compose_failed=True,
        )
        assert resolved == "constitutional fallback line"

    def test_export_includes_product_claim_provenance_flags(self) -> None:
        from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
            extract_reply_metadata_export,
        )

        exported = extract_reply_metadata_export(
            {
                "compose_source": "persona_llm",
                "response_mode": "llm",
                "chosen_path": "llm_reply",
                "llm_candidate_present": True,
                "final_text_transformed": True,
                "final_transform_reasons": ["product_claim_grounding_guard"],
                "product_claim_stripped": True,
                "product_claim_blocked": True,
                "product_claim_blocked_kinds": ["ungrounded_comparison"],
                "product_claim_recompose_requested": True,
                "product_claim_recompose_performed": True,
            },
        )
        assert exported["product_claim_stripped"] is True
        assert exported["product_claim_recompose_requested"] is True
        assert exported["product_claim_recompose_performed"] is True
        assert exported["product_claim_blocked"] is True

    def test_finalize_uses_recompose_candidate_not_original(self) -> None:
        from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
            finalize_post_guard_compose_provenance,
        )

        original = "الفستان أحلى من البديل."
        recomposed = "أقدر أرسل لك الخيارات المتوفرة."
        data = {
            "compose_source": "persona_llm",
            "llm_candidate_present": True,
            "compose_reply_candidate": original,
            "product_claim_original_compose_candidate": original,
            "product_claim_recompose_candidate": recomposed,
            "product_claim_recompose_performed": True,
        }
        data["compose_reply_candidate"] = recomposed
        finalize_post_guard_compose_provenance(
            data,
            final_text=recomposed,
            guard_replaced={"product_claim_grounding_guard": True},
        )
        assert data.get("final_customer_text_source") in {
            "persona_llm_postprocess",
            "llm_postprocess",
        }
        assert data["product_claim_original_compose_candidate"] == original


_UNGROUNDED_FASHION = "الفستان أحلى من البديل."
_UNGROUNDED_GENERIC = "الحذاء الرياضي أحلى من البديل."
_HONEY_DOMAIN_MARKERS = ("عسل", "سدر", "طلح", "حلاوة")
_GUARD_SOURCE_PATH = os.path.join(
    _backend,
    "modules",
    "ai",
    "brain",
    "postprocess",
    "product_claim_grounding_guard.py",
)


def _run_empty_strip_then_failed_recompose(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: int,
    first_reply: str,
    recomposed_reply: str,
    product_title: str,
) -> tuple[str, Dict[str, Any], MagicMock]:
    monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

    def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
        return _evidence(
            available_products=(
                {"id": 1, "title": product_title, "can_checkout": True},
            ),
            grounded_text_corpus="",
        )

    monkeypatch.setattr(
        "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
        _fake_build,
    )

    composer = MagicMock()
    composer.compose = AsyncMock(return_value=recomposed_reply)

    result_data: Dict[str, Any] = {
        "compose_source": "persona_llm",
        "response_mode": "llm",
        "chosen_path": "llm_reply",
        "compose_reply_candidate": first_reply,
        "catalog_fact_products": [{"id": 1, "title": product_title, "price": 120}],
        "products": [{"id": 1, "title": product_title, "price": 120}],
    }
    first = apply_product_claim_grounding_guard(
        reply=first_reply,
        tenant_id=tenant_id,
        catalog_fact_products=result_data["catalog_fact_products"],
        executor_products=result_data["products"],
    )
    stamp_product_claim_guard_provenance(result_data, first)
    assert first.requires_grounded_recompose is True
    assert first.scrubbed_empty is True

    result_data["product_claim_recompose_requested"] = True
    result_data["product_claim_original_compose_candidate"] = first_reply
    import asyncio

    recomposed = asyncio.run(composer.compose(None, None, None))
    result_data["product_claim_recompose_candidate"] = recomposed
    result_data["compose_reply_candidate"] = recomposed
    result_data["product_claim_recompose_performed"] = True

    second = apply_product_claim_grounding_guard(
        reply=recomposed,
        tenant_id=tenant_id,
        catalog_fact_products=result_data["catalog_fact_products"],
        executor_products=result_data["products"],
        inbound_metadata={"product_claim_recompose_performed": True},
        allow_recompose=False,
    )
    stamp_product_claim_guard_provenance(
        result_data,
        second,
        recompose_requested=True,
        recompose_performed=True,
    )
    assert second.requires_grounded_recompose is False
    assert second.scrubbed_empty is True

    final_reply = finalize_product_claim_after_authorized_recompose(
        second_pass=second,
        recomposed_reply=recomposed,
        result_data=result_data,
    )
    return final_reply, result_data, composer


class TestAID08B1FailedRecomposeConstitutionalFallback:
    def test_second_pass_empty_uses_existing_constitutional_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415
        from modules.ai.compose.constitutional_policy import (  # noqa: PLC0415
            validate_fallback_metadata,
        )
        from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
            extract_reply_metadata_export,
        )

        final_reply, result_data, composer = _run_empty_strip_then_failed_recompose(
            monkeypatch=monkeypatch,
            tenant_id=35,
            first_reply=_UNGROUNDED_FASHION,
            recomposed_reply=_UNGROUNDED_FASHION,
            product_title="فستان صيفي",
        )

        assert (final_reply or "").strip()
        assert _UNGROUNDED_FASHION not in final_reply
        assert "أحلى" not in final_reply
        for marker in _HONEY_DOMAIN_MARKERS:
            assert marker not in final_reply
        assert composer.compose.await_count == 1
        assert result_data["product_claim_recompose_count"] == 1
        assert result_data["product_claim_recompose_performed"] is True
        assert result_data["product_claim_constitutional_fallback"] is True
        assert result_data["compose_source"] == "fallback_deterministic"
        assert result_data["fallback_reason"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_REASON
        assert result_data["fallback_action_type"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_ACTION
        assert result_data["chosen_path"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_ACTION
        assert result_data["final_customer_text_source"] == "fallback_deterministic"
        assert is_compose_failure_fallback(final_reply)
        errors = validate_fallback_metadata(result_data, compose_attempted=True)
        assert errors == []
        exported = extract_reply_metadata_export(result_data)
        assert exported["compose_source"] == "fallback_deterministic"
        assert exported["product_claim_constitutional_fallback"] is True
        assert exported["product_claim_recompose_count"] == 1
        assert exported["fallback_reason"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_REASON

    def test_empty_compose_failure_also_uses_constitutional_fallback(
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
            reply=_UNGROUNDED_GENERIC,
            tenant_id=99,
        )
        assert first.requires_grounded_recompose is True
        result_data: Dict[str, Any] = {
            "compose_source": "persona_llm",
            "product_claim_recompose_performed": True,
        }
        second = apply_product_claim_grounding_guard(
            reply="",
            tenant_id=99,
            inbound_metadata={"product_claim_recompose_performed": True},
            allow_recompose=False,
        )
        final_reply = finalize_product_claim_after_authorized_recompose(
            second_pass=second,
            recomposed_reply="",
            result_data=result_data,
            compose_failed=True,
        )
        assert (final_reply or "").strip()
        assert _UNGROUNDED_GENERIC not in final_reply
        assert result_data["compose_source"] == "fallback_deterministic"
        assert result_data["product_claim_constitutional_fallback"] is True

    def test_tenant_isolation_and_no_guard_authored_safe_prose(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fashion_reply, fashion_data, fashion_composer = (
            _run_empty_strip_then_failed_recompose(
                monkeypatch=monkeypatch,
                tenant_id=35,
                first_reply=_UNGROUNDED_FASHION,
                recomposed_reply=_UNGROUNDED_FASHION,
                product_title="فستان صيفي",
            )
        )
        generic_reply, generic_data, generic_composer = (
            _run_empty_strip_then_failed_recompose(
                monkeypatch=monkeypatch,
                tenant_id=99,
                first_reply=_UNGROUNDED_GENERIC,
                recomposed_reply=_UNGROUNDED_GENERIC,
                product_title="حذاء رياضي أبيض",
            )
        )
        assert (fashion_reply or "").strip()
        assert (generic_reply or "").strip()
        assert "فستان" not in fashion_reply
        assert "حذاء" not in generic_reply
        assert _UNGROUNDED_FASHION not in fashion_reply
        assert _UNGROUNDED_GENERIC not in generic_reply
        assert fashion_composer.compose.await_count == 1
        assert generic_composer.compose.await_count == 1
        assert fashion_data["product_claim_recompose_count"] == 1
        assert generic_data["product_claim_recompose_count"] == 1

        with open(_GUARD_SOURCE_PATH, encoding="utf-8") as handle:
            source = handle.read()
        assert "_SAFE_" not in source
        assert "SAFE_NO_GROUNDED" not in source
        assert "أقدر أرسل لك الخيارات" not in source

    def test_recompose_exception_does_not_retry_semantic_pass(self) -> None:
        import asyncio

        composer = MagicMock()
        composer.compose = AsyncMock(side_effect=RuntimeError("compose boom"))
        text, failed, calls = asyncio.run(
            invoke_authorized_product_claim_recompose(composer, None, None, None),
        )
        assert composer.compose.await_count == 1
        assert calls == 1
        assert failed is True
        assert text == ""

    def test_latency_wrapper_failure_still_allows_one_compose(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio

        class _BoomScope:
            def __enter__(self) -> None:
                raise RuntimeError("latency wrapper boom")

            def __exit__(self, *_a: Any) -> bool:
                return False

        monkeypatch.setattr(
            "core.turn_latency.safe_compose_role_scope",
            lambda *_a, **_k: _BoomScope(),
        )
        composer = MagicMock()
        composer.compose = AsyncMock(return_value="أقدر أرسل لك الخيارات المتوفرة.")
        text, failed, calls = asyncio.run(
            invoke_authorized_product_claim_recompose(composer, None, None, None),
        )
        assert composer.compose.await_count == 1
        assert calls == 1
        assert failed is False
        assert text == "أقدر أرسل لك الخيارات المتوفرة."

    def test_brain_export_keeps_fallback_chosen_path(self) -> None:
        from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
            extract_reply_metadata_export,
        )

        second = ProductClaimGroundingGuardResult(
            reply="",
            action="stripped_empty",
            replaced=True,
            stripped=True,
            scrubbed_empty=True,
        )
        result_data: Dict[str, Any] = {
            "compose_source": "persona_llm",
            "response_mode": "llm",
            "chosen_path": "llm_reply",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
            "product_claim_recompose_performed": True,
            "product_claim_recompose_count": 1,
        }
        pipeline_chosen_path = "llm_reply"
        finalize_product_claim_after_authorized_recompose(
            second_pass=second,
            recomposed_reply=_UNGROUNDED_GENERIC,
            result_data=result_data,
        )
        if result_data.get("product_claim_constitutional_fallback"):
            pipeline_chosen_path = str(
                result_data.get("chosen_path") or pipeline_chosen_path
            ).strip() or pipeline_chosen_path
        exported = extract_reply_metadata_export(
            result_data,
            chosen_path=pipeline_chosen_path,
        )
        assert exported["chosen_path"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_ACTION
        assert exported["compose_source"] == "fallback_deterministic"
        assert exported["fallback_reason"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_REASON
        assert exported["fallback_action_type"] == PRODUCT_CLAIM_FAILED_COMPOSE_FALLBACK_ACTION
