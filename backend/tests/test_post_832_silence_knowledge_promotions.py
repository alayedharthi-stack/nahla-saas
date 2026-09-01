"""Post-#832 text-turn silence + merchant knowledge/promotion truth.

Protects semantic/state contracts, not exact Arabic customer wording.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_disabled_gate import (  # noqa: E402
    REASON_AI_PAUSED,
    REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
    disabled_reason_for_conversation,
    is_ai_allowed_by_store_mode,
    is_ai_disabled_for_conversation,
    persist_inbound_for_suppressed_turn,
)
from core.knowledge import apply_ai_visible_kb_query_filters, kb_row_is_ai_visible  # noqa: E402
from core.tenant import STORE_AI_MODE_TEST  # noqa: E402
from modules.ai.brain.commerce.knowledge_truth import (  # noqa: E402
    merge_knowledge_observability,
    overlay_kinds_held_by_structured_truth,
    structured_conflicts_for_kinds,
)
from modules.ai.brain.commerce.promotion_truth import (  # noqa: E402
    GENERATION_ABSENT,
    GENERATION_FAILED,
    NO_VALID_PROMOTIONS,
    PROMOTION_PARTIAL_FAILURE,
    PROMOTION_QUERY_FAILED,
    QUERY_OK,
    SOURCE_FAILED,
    SOURCE_NOT_QUERIED,
    SOURCE_OK,
    coupon_policy_for_compose,
    resolve_shareable_promotions,
)
from modules.ai.brain.compose.prompt_payload_slim import (  # noqa: E402
    resolve_kb_block_for_prompt,
)
from modules.ai.brain.types import BrainReplyState, INTENT_ASK_PRODUCT  # noqa: E402
from services.merchant_document_retrieval import (  # noqa: E402
    MAX_SECTIONS_PER_TURN,
    retrieve_merchant_documents,
)
from modules.ai.prompts import tenant_overlay as overlay_mod  # noqa: E402
from utils.phone_utils import (  # noqa: E402
    normalize_whatsapp_phone_for_ai_allowlist,
    phone_matches_ai_test_allowlist,
)


ALLOWED = "966537970430"
FORMATS = ("0537970430", "+966537970430", "966537970430", "537970430")


def _settings(ai):
    return SimpleNamespace(ai_settings=ai)


def _convo(**kwargs):
    defaults = dict(
        id=26,
        tenant_id=1,
        customer_id=7,
        ai_paused=False,
        ai_paused_reason=None,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        status="active",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestTestModeParityAndSilence:
    def test_phone_formats_share_one_canonical_identity(self) -> None:
        allowlist = [ALLOWED]
        canonical = {
            normalize_whatsapp_phone_for_ai_allowlist(raw) for raw in FORMATS
        }
        assert canonical == {ALLOWED}
        for raw in FORMATS:
            assert phone_matches_ai_test_allowlist(raw, allowlist) is True

    def test_authorized_test_phone_reaches_brain_despite_notify_handoff(self) -> None:
        db = MagicMock()
        convo = _convo(
            is_human_handoff=True,
            handoff_active=True,
            needs_human=True,
            status="active",
        )
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            status="active", handoff_reason="customer_request",
        )
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings({
                "store_ai_mode": STORE_AI_MODE_TEST,
                "store_ai_enabled": False,
                "ai_test_allowed_numbers": [ALLOWED],
            }),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            for raw in FORMATS:
                mode = is_ai_allowed_by_store_mode(db, 1, raw)
                assert mode.allowed is True
                decision = is_ai_disabled_for_conversation(
                    db, tenant_id=1, customer_phone=raw,
                )
                assert decision.disabled is False
                assert disabled_reason_for_conversation(convo) == ""

    def test_unauthorized_test_phone_is_explicitly_suppressed(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings({
                "store_ai_mode": STORE_AI_MODE_TEST,
                "store_ai_enabled": False,
                "ai_test_allowed_numbers": [ALLOWED],
            }),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[_convo()],
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=1, customer_phone="966500000099",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_STORE_AI_TEST_MODE_NOT_ALLOWED

    def test_genuine_takeover_residue_does_not_disable(self) -> None:
        convo = _convo(
            status="human",
            paused_by_human=True,
            taken_over_at=datetime.now(timezone.utc),
        )
        assert disabled_reason_for_conversation(convo) == ""

    def test_suppressed_inbound_records_explicit_reason(self) -> None:
        db = MagicMock()
        convo = _convo(status="active")
        saved = {}

        def _save(*_a, extra_metadata=None, **_k):
            saved.update(extra_metadata or {})
            return None

        with patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "core.conversation_engine.StateManager.save_message",
            side_effect=_save,
        ):
            persist_inbound_for_suppressed_turn(
                db,
                tenant_id=1,
                customer_phone=ALLOWED,
                inbound_body="text",
                suppression_reason=REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
            )
        assert saved.get("ai_disabled_gate") is True
        assert saved.get("ai_disabled_reason") == REASON_STORE_AI_TEST_MODE_NOT_ALLOWED


class TestKnowledgeTenantScope:
    def test_overlay_query_filters_tenant_at_source(self) -> None:
        src = open(overlay_mod.__file__, encoding="utf-8").read()
        assert "MerchantKnowledgeSection.tenant_id == tenant_id" in src
        assert "apply_ai_visible_kb_query_filters" in src

    def test_ai_visible_filter_excludes_inactive(self) -> None:
        query = MagicMock()
        query.filter.return_value = query
        apply_ai_visible_kb_query_filters(query)
        assert query.filter.called

    def test_needs_review_and_inactive_are_not_customer_visible(self) -> None:
        assert kb_row_is_ai_visible(SimpleNamespace(
            deleted_at=None, is_active=True, ai_status="approved",
        )) is True
        assert kb_row_is_ai_visible(SimpleNamespace(
            deleted_at=None, is_active=False, ai_status="approved",
        )) is False
        assert kb_row_is_ai_visible(SimpleNamespace(
            deleted_at=None, is_active=True, ai_status="needs_review",
        )) is False

    def test_structured_kind_retrieves_without_keyword_message(self) -> None:
        section = SimpleNamespace(
            id=11,
            kind="store_story",
            title="story",
            body="Generic merchant founded the shop to sell cotton shirts.",
            source="manual",
            metadata_json={},
            product_links=[],
        )
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = [section]
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ), patch(
            "services.merchant_knowledge_customer_readiness.mks_section_customer_ready",
            return_value=SimpleNamespace(is_ready=True, reason_code=""),
        ):
            result = retrieve_merchant_documents(
                db, 1, "hello there", structured_kind="store_story",
            )
        assert result.knowledge_query_run is True
        assert result.tenant_id == 1
        assert result.selected_knowledge_ids == (11,)
        assert result.sections[0].kind == "store_story"

    def test_unrelated_text_does_not_dump_long_form_kb(self) -> None:
        db = MagicMock()
        result = retrieve_merchant_documents(db, 1, "ok")
        assert result.sections == ()
        assert result.knowledge_query_run is False
        db.query.assert_not_called()

    def test_raw_customer_text_does_not_activate_semantic_kb_retrieval(self) -> None:
        db = MagicMock()
        for text in ("shipping policy?", "return policy?", "our story", "faq"):
            result = retrieve_merchant_documents(db, 1, text)
            assert result.sections == ()
            assert result.knowledge_query_run is False
            assert result.matched_intent == ""
        db.query.assert_not_called()

    def test_same_structured_kind_ignores_customer_wording(self) -> None:
        section = SimpleNamespace(
            id=22,
            kind="shipping_policy",
            title="ship",
            body="Generic merchant ships cotton shirts within three days.",
            source="manual",
            metadata_json={},
            product_links=[],
        )
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = [section]
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ), patch(
            "services.merchant_knowledge_customer_readiness.mks_section_customer_ready",
            return_value=SimpleNamespace(is_ready=True, reason_code=""),
        ):
            ids = []
            for wording in ("one formulation", "another formulation", ""):
                result = retrieve_merchant_documents(
                    db, 1, wording, structured_kind="shipping_policy",
                )
                assert result.matched_intent == "shipping_policy"
                ids.append(result.selected_knowledge_ids)
        assert ids == [(22,), (22,), (22,)]

    def test_customer_runtime_does_not_call_text_intent_detector(self) -> None:
        import services.merchant_document_retrieval as retrieval_mod

        src = open(retrieval_mod.__file__, encoding="utf-8").read()
        fn = src.split("def retrieve_merchant_documents", 1)[1].split("\ndef ", 1)[0]
        assert "detect_document_retrieval_intent" not in fn

    def test_faq_retrieved_only_with_brain_kind(self) -> None:
        section = SimpleNamespace(
            id=33,
            kind="faq",
            title="faq",
            body="Generic merchant FAQ: cotton shirts ship from Riyadh.",
            source="manual",
            metadata_json={},
            product_links=[],
        )
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = [section]
        empty = retrieve_merchant_documents(db, 1, "faq please")
        assert empty.sections == ()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ), patch(
            "services.merchant_knowledge_customer_readiness.mks_section_customer_ready",
            return_value=SimpleNamespace(is_ready=True, reason_code=""),
        ):
            result = retrieve_merchant_documents(
                db, 1, "faq please", structured_kind="faq",
            )
        assert result.matched_intent == "faq"
        assert result.selected_knowledge_ids == (33,)

    def test_structured_catalog_facts_win_over_kb_kinds(self) -> None:
        conflicts = structured_conflicts_for_kinds(
            ["product_price"],
            has_catalog=True,
        )
        assert "catalog_structured_wins" in conflicts
        assert "catalog_structured_wins" not in structured_conflicts_for_kinds(
            ["product_usage"],
            has_catalog=True,
        )

    def test_knowledge_observability_contract(self) -> None:
        obs = merge_knowledge_observability(
            tenant_id=33,
            overlay_ids=[1],
            retrieval_ids=[2],
            candidate_count=4,
            retrieved_kinds=["shipping_policy"],
        )
        payload = obs.to_dict()
        assert payload["knowledge_query_run"] is True
        assert payload["tenant_id"] == 33
        assert payload["selected_knowledge_ids"] == [1, 2]
        assert payload["model_visible_knowledge_ids"] == [1, 2]
        assert payload["source_section"] == "overlay+retrieval"

    def test_cross_tenant_resolver_does_not_scan_then_filter(self) -> None:
        src = open(overlay_mod.__file__, encoding="utf-8").read()
        facts_fn = src.split("def build_structured_facts_block", 1)[1].split(
            "\ndef ", 1
        )[0]
        tenant_filter_at = facts_fn.find("tenant_id == tenant_id")
        all_at = facts_fn.find(".all()")
        assert tenant_filter_at != -1
        assert all_at != -1
        assert tenant_filter_at < all_at


class _CouponRow:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.tenant_id = kwargs.get("tenant_id", 1)
        self.code = kwargs.get("code", "SAVE10")
        self.description = kwargs.get("description", "10 percent")
        self.discount_type = kwargs.get("discount_type", "percentage")
        self.discount_value = kwargs.get("discount_value", "10")
        self.expires_at = kwargs.get("expires_at")
        self.source_type = kwargs.get("source_type", "manual")
        self.allocation_channel = kwargs.get("allocation_channel", "")
        self.extra_metadata = kwargs.get("extra_metadata") or {}
        self.rules = kwargs.get("rules") or []
        self.starts_at = kwargs.get("starts_at")


class _PromoRow:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 9)
        self.tenant_id = kwargs.get("tenant_id", 1)
        self.name = kwargs.get("name", "Summer offer")
        self.description = kwargs.get("description", "10 percent off shirts")
        self.promotion_type = kwargs.get("promotion_type", "percentage")
        self.discount_value = kwargs.get("discount_value", "10")
        self.conditions = kwargs.get("conditions") or {"min_order_amount": 100}
        self.starts_at = kwargs.get("starts_at")
        self.ends_at = kwargs.get("ends_at")
        self.status = kwargs.get("status", "active")
        self.usage_count = kwargs.get("usage_count", 0)
        self.usage_limit = kwargs.get("usage_limit")


class TestPromotionTruth:
    def _query(self, coupons, promos=None):
        db = MagicMock()

        def _query(model):
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            q.join.return_value = q
            name = str(getattr(model, "__name__", "") or model)
            if "CouponRule" in name:
                q.all.return_value = []
                q.first.return_value = None
            elif "Promotion" in name:
                q.all.return_value = list(promos or [])
                q.first.return_value = None
            else:
                q.all.return_value = list(coupons)
                q.first.return_value = None
            return q

        db.query.side_effect = _query
        return db

    def test_active_native_coupon_is_structured(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([_CouponRow(expires_at=future)])
        result = resolve_shareable_promotions(db, 1)
        assert result.query_run is True
        assert result.invented_codes is False
        assert result.generation_authorized is False
        assert result.shareable
        assert result.shareable[0]["code"] == "SAVE10"
        assert result.shareable[0]["discount_value"] == "10"
        assert result.shareable[0]["eligibility_determined"] is False

    def test_expired_coupon_not_offered(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        db = self._query([_CouponRow(code="OLD", expires_at=past)])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []

    def test_disabled_metadata_not_offered(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(expires_at=future, extra_metadata={"status": "disabled"}),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []

    def test_conditions_are_exposed(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(
                expires_at=future,
                extra_metadata={"min_order_amount": 150, "usage_limit": 20},
            ),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable[0]["conditions"]["min_order_amount"] == 150
        assert result.shareable[0]["conditions"]["usage_limit"] == 20

    def test_provider_synced_uses_same_path(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(code="SALLA10", source_type="salla", expires_at=future),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable[0]["code"] == "SALLA10"
        assert result.shareable[0]["source_type"] == "salla"

    def test_multiple_valid_coupons_available(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(id=1, code="A", expires_at=future),
            _CouponRow(id=2, code="B", expires_at=future),
        ])
        result = resolve_shareable_promotions(db, 77)
        codes = {item["code"] for item in result.shareable}
        assert codes == {"A", "B"}
        assert result.tenant_id == 77

    def test_query_is_tenant_scoped_at_source(self) -> None:
        db = self._query([])
        resolve_shareable_promotions(db, 33)
        assert db.query.called

    def test_no_coupon_does_not_invent_code(self) -> None:
        db = self._query([])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []
        assert result.invented_codes is False
        assert result.query_failed is False
        assert result.query_outcome == NO_VALID_PROMOTIONS
        assert result.coupon_source == SOURCE_OK
        assert result.offer_source == SOURCE_OK
        assert result.generation_rules_state == GENERATION_ABSENT
        assert result.generation_rules_present is False

    def test_coupon_query_failure_is_not_empty_catalog(self) -> None:
        db = MagicMock()

        def _query(model):
            name = str(getattr(model, "__name__", "") or model)
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            q.join.return_value = q
            if name == "Coupon":
                raise RuntimeError("coupon unavailable")
            q.all.return_value = []
            q.first.return_value = None
            return q

        db.query.side_effect = _query
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []
        assert result.offers == []
        assert result.query_failed is True
        assert result.query_outcome == PROMOTION_QUERY_FAILED
        assert result.coupon_source == SOURCE_FAILED
        assert result.offer_source == SOURCE_OK
        assert result.invented_codes is False

    def test_eligibility_undetermined_is_not_guaranteed(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([_CouponRow(expires_at=future)])
        result = resolve_shareable_promotions(db, 1)
        assert result.query_outcome == QUERY_OK
        assert result.shareable[0]["eligibility_determined"] is False
        assert result.shareable[0]["eligibility_note"] == "conditions_not_fully_evaluated"

    def test_offer_query_failure_with_empty_coupons_is_not_no_promotions(self) -> None:
        db = MagicMock()

        def _query(model):
            name = getattr(model, "__name__", "") or str(model)
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            if name == "Coupon":
                q.all.return_value = []
                return q
            if name == "Promotion":
                raise RuntimeError("offers unavailable")
            q.all.return_value = []
            q.first.return_value = None
            return q

        db.query.side_effect = _query
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []
        assert result.offers == []
        assert result.query_failed is True
        assert result.query_outcome == PROMOTION_QUERY_FAILED
        assert result.invented_codes is False

    def test_offer_query_failure_keeps_valid_coupons(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = MagicMock()

        def _query(model):
            name = getattr(model, "__name__", "") or str(model)
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            if name == "Coupon":
                q.all.return_value = [_CouponRow(expires_at=future)]
                return q
            if name == "Promotion":
                raise RuntimeError("offers unavailable")
            q.all.return_value = []
            q.first.return_value = None
            return q

        db.query.side_effect = _query
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable[0]["code"] == "SAVE10"
        assert result.query_failed is True
        assert result.query_outcome == PROMOTION_PARTIAL_FAILURE
        assert result.offer_source == SOURCE_FAILED
        assert result.coupon_source == SOURCE_OK

    def test_active_offer_has_no_invented_code(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([], promos=[_PromoRow(ends_at=future)])
        result = resolve_shareable_promotions(db, 1)
        assert result.offers
        assert result.offers[0]["code"] == ""
        assert result.offers[0]["record_kind"] == "offer"
        assert result.generation_authorized is False

    def test_inactive_offer_not_listed(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([], promos=[_PromoRow(status="paused", ends_at=future)])
        result = resolve_shareable_promotions(db, 1)
        assert result.offers == []

    def test_resolver_never_calls_generator(self) -> None:
        src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "promotion_truth.py"),
            encoding="utf-8",
        ).read()
        assert "CouponGenerator" not in src
        assert "materialise_for_customer" not in src

    def test_coupon_failure_keeps_valid_offer(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = MagicMock()

        def _query(model):
            name = str(getattr(model, "__name__", "") or model)
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            q.join.return_value = q
            if name == "Coupon":
                raise RuntimeError("coupon unavailable")
            if "CouponRule" in name:
                q.first.return_value = None
                q.all.return_value = []
                return q
            q.all.return_value = [_PromoRow(ends_at=future)]
            q.first.return_value = None
            return q

        db.query.side_effect = _query
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []
        assert result.offers
        assert result.offers[0]["code"] == ""
        assert result.coupon_source == SOURCE_FAILED
        assert result.offer_source == SOURCE_OK
        assert result.query_outcome == PROMOTION_PARTIAL_FAILURE
        assert result.query_failed is True
        assert result.invented_codes is False

    def test_coupon_rule_lookup_failure_is_unknown_not_false(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = MagicMock()

        def _query(model):
            name = str(getattr(model, "__name__", "") or model)
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            q.join.return_value = q
            if "CouponRule" in name:
                raise RuntimeError("rules unavailable")
            if name == "Promotion":
                q.all.return_value = []
                q.first.return_value = None
                return q
            q.all.return_value = [_CouponRow(expires_at=future)]
            q.first.return_value = None
            return q

        db.query.side_effect = _query
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable[0]["code"] == "SAVE10"
        assert result.generation_rules_present is None
        assert result.generation_rules_state == GENERATION_FAILED
        assert result.generation_rule_source == SOURCE_FAILED
        assert result.generation_authorized is False
        assert result.query_outcome == PROMOTION_PARTIAL_FAILURE

    def test_globally_exhausted_coupon_is_excluded(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(
                code="USEDUP",
                expires_at=future,
                extra_metadata={"usage_count": 5, "usage_limit": 5},
            ),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []
        assert result.query_outcome == NO_VALID_PROMOTIONS
        assert result.coupon_source == SOURCE_OK

    def test_exhausted_offer_is_excluded(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([], promos=[
            _PromoRow(ends_at=future, usage_count=10, usage_limit=10),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.offers == []
        assert result.query_outcome == NO_VALID_PROMOTIONS

    def test_poisoned_session_does_not_query_later_sources(self) -> None:
        class PendingRollbackError(Exception):
            pass

        db = MagicMock()
        calls: list[str] = []

        def _query(model):
            name = str(getattr(model, "__name__", "") or model)
            calls.append(name)
            raise PendingRollbackError("current transaction is aborted")

        db.query.side_effect = _query
        result = resolve_shareable_promotions(db, 1)
        assert result.coupon_source == SOURCE_FAILED
        assert result.offer_source == SOURCE_NOT_QUERIED
        assert result.generation_rule_source == SOURCE_NOT_QUERIED
        assert result.query_outcome == PROMOTION_QUERY_FAILED
        assert len(calls) == 1


def _mock_db_no_handoff_session() -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def _kb_section(section_id: int, kind: str, body: str = "Generic merchant policy text.") -> SimpleNamespace:
    return SimpleNamespace(
        id=section_id,
        kind=kind,
        title=kind,
        body=body,
        source="manual",
        metadata_json={},
        product_links=[],
    )


def _retrieve_with_structured_kind(
    db: MagicMock,
    tenant_id: int,
    structured_kind: str,
    rows: list,
    *,
    message: str = "",
) -> object:
    q = db.query.return_value
    q.filter.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.all.return_value = list(rows)
    with patch(
        "core.knowledge.apply_ai_visible_kb_query_filters",
        side_effect=lambda query: query,
    ), patch(
        "services.merchant_knowledge_customer_readiness.mks_section_customer_ready",
        return_value=SimpleNamespace(is_ready=True, reason_code=""),
    ):
        return retrieve_merchant_documents(
            db, tenant_id, message, structured_kind=structured_kind,
        )


class TestSuiteAGateSuppression:
    def test_needs_human_alone_does_not_disable_ai(self) -> None:
        convo = _convo(needs_human=True)
        assert disabled_reason_for_conversation(convo) == ""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=5, status="active", handoff_reason="customer_request",
        )
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=1, customer_phone="966500000001",
            )
        assert decision.disabled is False

    def test_ai_paused_true_disables(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_AI_PAUSED)
        assert disabled_reason_for_conversation(convo) == REASON_AI_PAUSED
        db = _mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=1, customer_phone="966500000001",
            )
        assert decision.disabled is True
        assert decision.reason == REASON_AI_PAUSED

    def test_ownership_handoff_active_does_not_disable(self) -> None:
        convo = _convo()
        db = _mock_db_no_handoff_session()
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=True,
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=1, customer_phone="966500000001",
            )
        assert decision.disabled is False

    def test_paused_by_human_does_not_disable(self) -> None:
        convo = _convo(paused_by_human=True)
        assert disabled_reason_for_conversation(convo) == ""

    def test_status_human_alone_does_not_disable(self) -> None:
        convo = _convo(status="human")
        assert disabled_reason_for_conversation(convo) == ""

    def test_taken_over_at_does_not_disable(self) -> None:
        convo = _convo(taken_over_at=datetime.now(timezone.utc))
        assert disabled_reason_for_conversation(convo) == ""

    def test_advisory_handoff_flags_alone_do_not_disable(self) -> None:
        convo = _convo(is_human_handoff=True, handoff_active=True)
        assert disabled_reason_for_conversation(convo) == ""

    def test_prefixed_notify_session_does_not_disable(self) -> None:
        from core.ai_disabled_gate import _handoff_session_disables_ai

        row = SimpleNamespace(
            status="active",
            handoff_reason="customer_request_pre_brain:clear",
        )
        assert _handoff_session_disables_ai(row) is False
        leftover = SimpleNamespace(
            status="active",
            handoff_reason="customer_request_outer_exception",
        )
        assert _handoff_session_disables_ai(leftover) is False

    def test_staff_takeover_session_does_not_disable(self) -> None:
        from core.ai_disabled_gate import _handoff_session_disables_ai

        row = SimpleNamespace(status="active", handoff_reason="staff_takeover")
        assert _handoff_session_disables_ai(row) is False


class TestSuiteDKnowledgeRetrieval:
    def test_brain_store_story_kind_retrieves(self) -> None:
        db = MagicMock()
        result = _retrieve_with_structured_kind(
            db, 1, "store_story", [_kb_section(41, "store_story")],
        )
        assert result.knowledge_query_run is True
        assert result.matched_intent == "store_story"
        assert result.selected_knowledge_ids == (41,)

    def test_brain_shipping_policy_kind_retrieves(self) -> None:
        db = MagicMock()
        result = _retrieve_with_structured_kind(
            db, 1, "shipping_policy", [_kb_section(42, "shipping_policy")],
        )
        assert result.knowledge_query_run is True
        assert result.matched_intent == "shipping_policy"
        assert result.selected_knowledge_ids == (42,)

    def test_large_kb_bounded_to_max_sections_per_turn(self) -> None:
        rows = [
            _kb_section(i, "return_policy", body=f"Policy chunk {i} " * 20)
            for i in range(1, 9)
        ]
        db = MagicMock()
        result = _retrieve_with_structured_kind(
            db, 1, "return_policy", rows,
        )
        assert result.knowledge_query_run is True
        assert len(result.sections) <= MAX_SECTIONS_PER_TURN
        assert result.candidate_count == 8

    def test_empty_kb_with_structured_kind_runs_query(self) -> None:
        db = MagicMock()
        result = _retrieve_with_structured_kind(db, 1, "faq", [])
        assert result.sections == ()
        assert result.knowledge_query_run is True
        assert result.matched_intent == "faq"

    def test_empty_kb_without_structured_kind_skips_query(self) -> None:
        db = MagicMock()
        result = retrieve_merchant_documents(db, 1, "")
        assert result.sections == ()
        assert result.knowledge_query_run is False
        db.query.assert_not_called()

    def test_knowledge_query_failure_is_observable(self) -> None:
        db = MagicMock()
        db.query.side_effect = RuntimeError("kb unavailable")
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(
                db, 1, "", structured_kind="store_story",
            )
        assert result.sections == ()
        assert result.knowledge_query_run is True
        assert result.query_failed is True
        assert result.matched_intent == "store_story"


class TestSuiteCWebhookSuppressionReason:
    def test_webhook_persist_sites_pass_suppression_reason(self) -> None:
        src = open(
            os.path.join(_BACKEND, "routers", "whatsapp_webhook.py"),
            encoding="utf-8",
        ).read()
        marker = "persist_inbound_for_suppressed_turn("
        starts = []
        idx = 0
        while True:
            found = src.find(marker, idx)
            if found == -1:
                break
            starts.append(found)
            idx = found + len(marker)
        assert len(starts) >= 2
        for start in starts:
            snippet = src[start:start + 700]
            assert "suppression_reason=" in snippet


class TestSuiteEStructuredConflicts:
    def test_branches_structured_wins(self) -> None:
        conflicts = structured_conflicts_for_kinds(
            ["branch"], has_branches=True,
        )
        assert conflicts == ["branches_structured_wins"]

    def test_contacts_structured_wins(self) -> None:
        conflicts = structured_conflicts_for_kinds(
            ["staff_contact"], has_contacts=True,
        )
        assert conflicts == ["contacts_structured_wins"]

    def test_promotions_structured_wins(self) -> None:
        conflicts = structured_conflicts_for_kinds(
            ["promotion"], has_promotions=True,
        )
        assert conflicts == ["promotions_structured_wins"]

    def test_payments_structured_wins(self) -> None:
        conflicts = structured_conflicts_for_kinds(
            ["payment_method"], has_payments=True,
        )
        assert conflicts == ["payments_structured_wins"]

    def test_overlay_holds_operational_kinds_not_explanations(self) -> None:
        held = overlay_kinds_held_by_structured_truth(
            has_catalog=True,
            has_branches=True,
            has_payments=True,
            has_promotions=True,
        )
        assert "branches" in held
        assert "payment_method" in held
        assert "coupon" in held
        assert "product_price" in held
        assert "product_usage" not in held
        assert "product_recipe" not in held
        assert "store_story" not in held

    def test_overlay_holds_nothing_without_structured_owners(self) -> None:
        held = overlay_kinds_held_by_structured_truth()
        assert held == frozenset()


class TestSuiteFPromotionTruth:
    def _query(self, coupons, promos=None):
        return TestPromotionTruth()._query(coupons, promos=promos)

    def test_future_starts_at_in_metadata_excluded(self) -> None:
        future_expiry = datetime.now(timezone.utc) + timedelta(days=7)
        future_start = datetime.now(timezone.utc) + timedelta(days=2)
        db = self._query([
            _CouponRow(
                code="LATER",
                expires_at=future_expiry,
                extra_metadata={"starts_at": future_start},
            ),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []

    def test_conditions_project_min_order_products_categories_limit(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(
                expires_at=future,
                extra_metadata={
                    "min_order_amount": 200,
                    "product_ids": [11, 12],
                    "category_ids": [3],
                    "per_customer_limit": 1,
                },
            ),
        ])
        result = resolve_shareable_promotions(db, 1)
        conditions = result.shareable[0]["conditions"]
        assert conditions["min_order_amount"] == 200
        assert conditions["product_ids"] == [11, 12]
        assert conditions["category_ids"] == [3]
        assert conditions["per_customer_limit"] == 1

    def test_three_active_coupons_all_appear(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(id=1, code="T33A", expires_at=future),
            _CouponRow(id=2, code="T33B", expires_at=future),
            _CouponRow(id=3, code="T33C", expires_at=future),
        ])
        result = resolve_shareable_promotions(db, 1)
        codes = {item["code"] for item in result.shareable}
        assert codes == {"T33A", "T33B", "T33C"}

    def test_campaign_allocation_channel_excluded(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query([
            _CouponRow(
                code="CAMPAIGN10",
                expires_at=future,
                allocation_channel="campaign",
            ),
        ])
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []

    def test_cross_tenant_result_carries_requested_tenant_id(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        coupons = [_CouponRow(id=1, code="SHARED", expires_at=future)]
        db = self._query(coupons)
        result_ten = resolve_shareable_promotions(db, 10)
        result_twenty = resolve_shareable_promotions(db, 20)
        assert result_ten.tenant_id == 10
        assert result_twenty.tenant_id == 20

    def test_scalar_product_ids_do_not_abort_offer_source(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)
        db = self._query(
            [_CouponRow(
                expires_at=future,
                extra_metadata={"product_ids": 11},
            )],
            promos=[_PromoRow(ends_at=future)],
        )
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable[0]["code"] == "SAVE10"
        assert result.shareable[0]["conditions"]["product_ids"] == [11]
        assert result.offers
        assert result.coupon_source == SOURCE_OK
        assert result.offer_source == SOURCE_OK

    def test_malformed_coupon_row_does_not_hide_valid_offer(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=7)

        class _BoomRow:
            def __init__(self, **kwargs):
                self.id = 1
                self.tenant_id = 1
                self.code = "BOOM"
                self.description = ""
                self.discount_type = "percentage"
                self.discount_value = "10"
                self.expires_at = kwargs.get("expires_at")
                self.source_type = "manual"
                self.allocation_channel = ""
                self.rules = []
                self.starts_at = None

            @property
            def extra_metadata(self):
                raise TypeError("scalar json product_ids")

        db = self._query(
            [_BoomRow(expires_at=future)],
            promos=[_PromoRow(ends_at=future)],
        )
        result = resolve_shareable_promotions(db, 1)
        assert result.shareable == []
        assert result.offers
        assert result.offer_source == SOURCE_OK
        assert result.coupon_source == SOURCE_OK

    def test_compose_policy_does_not_treat_source_failure_as_empty(self) -> None:
        facts = SimpleNamespace(
            has_coupons=False,
            coupon_eligibility="",
            shareable_promotions=[],
            shareable_offers=[],
            promotion_query_outcome=PROMOTION_QUERY_FAILED,
            promotion_query_failed=True,
            promotion_coupon_source=SOURCE_FAILED,
            promotion_offer_source=SOURCE_OK,
            promotion_generation_rule_source=SOURCE_OK,
            generation_rules_state=GENERATION_ABSENT,
        )
        policy = coupon_policy_for_compose(facts)
        assert policy["query_failed"] is True
        assert policy["query_outcome"] == PROMOTION_QUERY_FAILED
        assert policy["no_valid_promotions"] is False
        assert policy["generation_authorized"] is False
        assert policy["invented_codes"] is False

    def test_compose_policy_keeps_generation_unknown_distinct_from_false(self) -> None:
        facts = SimpleNamespace(
            has_coupons=True,
            coupon_eligibility="",
            shareable_promotions=[{"code": "SAVE10"}],
            shareable_offers=[],
            promotion_query_outcome=PROMOTION_PARTIAL_FAILURE,
            promotion_query_failed=True,
            promotion_coupon_source=SOURCE_OK,
            promotion_offer_source=SOURCE_OK,
            promotion_generation_rule_source=SOURCE_FAILED,
            generation_rules_state=GENERATION_FAILED,
        )
        policy = coupon_policy_for_compose(facts)
        assert policy["generation_rules_state"] == GENERATION_FAILED
        assert "generation_rules_present" not in policy
        assert policy["generation_authorized"] is False

    def test_healthy_empty_is_no_valid_promotions(self) -> None:
        facts = SimpleNamespace(
            has_coupons=False,
            coupon_eligibility="",
            shareable_promotions=[],
            shareable_offers=[],
            promotion_query_outcome=NO_VALID_PROMOTIONS,
            promotion_query_failed=False,
            promotion_coupon_source=SOURCE_OK,
            promotion_offer_source=SOURCE_OK,
            promotion_generation_rule_source=SOURCE_OK,
            generation_rules_state=GENERATION_ABSENT,
        )
        policy = coupon_policy_for_compose(facts)
        assert policy["no_valid_promotions"] is True
        assert policy["query_failed"] is False


class TestStructuredOverlayDoesNotFallBackToLegacy:
    def test_held_empty_skips_legacy_overlay_facts(self) -> None:
        state = BrainReplyState(
            store_name="generic shop",
            intent_name=INTENT_ASK_PRODUCT,
            merchant_context={"structured_overlay_held_empty": True},
        )
        block = resolve_kb_block_for_prompt(
            state,
            structured_kb="",
            overlay_facts="كوبون وهمي SAVE99 من المعرفة اليدوية",
        )
        assert block == ""

    def test_unmigrated_merchant_still_uses_overlay_facts(self) -> None:
        state = BrainReplyState(
            store_name="generic shop",
            intent_name=INTENT_ASK_PRODUCT,
            merchant_context={"structured_overlay_held_empty": False},
        )
        block = resolve_kb_block_for_prompt(
            state,
            structured_kb="",
            overlay_facts="نشحن القمصان خلال يومين",
        )
        assert "نشحن القمصان خلال يومين" in block

    def test_overlay_hold_stamps_held_empty(self) -> None:
        section = SimpleNamespace(
            id=7,
            kind="coupon",
            title="promo",
            body="SAVE99",
            priority=1,
            updated_at=None,
            product_links=[],
            media_links=[],
        )
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value = q
        q.order_by.return_value = q
        q.all.return_value = [section]
        obs: dict = {}
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            text = overlay_mod.build_structured_facts_block(
                db, 1, observability_out=obs, has_promotions=True,
            )
        assert text == ""
        assert obs.get("structured_overlay_held_empty") is True
        assert "SAVE99" not in text
