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
    REASON_HUMAN_SUPERVISION,
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
    structured_conflicts_for_kinds,
)
from modules.ai.brain.commerce.promotion_truth import (  # noqa: E402
    resolve_shareable_promotions,
)
from modules.ai.prompts import tenant_overlay as overlay_mod  # noqa: E402
from services.merchant_document_retrieval import retrieve_merchant_documents  # noqa: E402
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

    def test_genuine_takeover_remains_suppressed(self) -> None:
        convo = _convo(
            status="human",
            paused_by_human=True,
            taken_over_at=datetime.now(timezone.utc),
        )
        assert disabled_reason_for_conversation(convo) == REASON_HUMAN_SUPERVISION

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

    def test_structured_catalog_facts_win_over_kb_kinds(self) -> None:
        conflicts = structured_conflicts_for_kinds(
            ["product_usage"],
            has_catalog=True,
        )
        assert "catalog_structured_wins" in conflicts

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
            name = getattr(model, "__name__", "") or str(model)
            if name == "Promotion":
                q.all.return_value = list(promos or [])
                q.first.return_value = None
            elif name == "CouponRule":
                q.all.return_value = []
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
