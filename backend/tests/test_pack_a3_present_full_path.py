"""
Pack A3 — PRESENT full-path Brain+Compose transport proof (no billing bypass).

Seeds a real active BillingSubscription so has_billing_access returns True
naturally. Stubs only external boundaries (LLM compose + WA quota check).
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from models import BillingPlan, BillingSubscription  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    make_scenario_db,
    seed_knowledge_section,
    seed_tenant,
)

_PHONE = "966500000833"
_SAFE_RETURN_BODY = (
    "يمكن الاسترجاع أو الاستبدال خلال 7 أيام من تاريخ الاستلام "
    "بشرط أن يكون المنتج بحالته الأصلية. للتقديم تواصل معنا عبر الواتساب."
)
_PLACEHOLDER_BODY = (
    "نقبل الاسترجاع خلال [أضف المدة — مثلاً 14 يوماً] من تاريخ الاستلام."
)


def _run(coro):
    return asyncio.run(coro)


def _seed_billing(db, tenant_id: int) -> None:
    plan = BillingPlan(
        id=9101,
        tenant_id=None,
        slug="starter-a3-present",
        name="Starter",
        description="test",
        currency="SAR",
        price_sar=899,
        billing_cycle="monthly",
        features=[],
        limits={},
    )
    db.merge(plan)
    db.flush()
    sub = BillingSubscription(
        id=9101,
        tenant_id=tenant_id,
        plan_id=9101,
        status="active",
        started_at=datetime.now(timezone.utc) - timedelta(days=3),
        ends_at=datetime.now(timezone.utc) + timedelta(days=27),
    )
    db.merge(sub)
    db.commit()


class TestPackA3PresentFullPathNoBillingBypass:
    def test_present_return_policy_brain_compose_consumes_body(self):
        from modules.ai.brain.pipeline import get_brain
        from core.billing import has_billing_access
        from tests.commerce_scenario_fixtures import seed_customer

        db, _engine = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        _seed_billing(db, tenant.id)
        assert has_billing_access(db, tenant.id) is True
        seed_customer(db, tenant.id, phone=_PHONE)

        section = seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            title="سياسة الاسترجاع",
            body=_SAFE_RETURN_BODY,
            priority=100,
        )
        assert section.id

        brain = get_brain()
        captured: Dict[str, Any] = {}

        async def _compose_capture(decision, result, ctx):  # noqa: ANN001
            rs = getattr(ctx, "reply_state", None)
            known = dict(getattr(rs, "known_facts", None) or {})
            captured["decision_topic"] = str((decision.args or {}).get("topic") or "")
            captured["policy_surface"] = (decision.args or {}).get("policy_surface")
            captured["merchant_policy_status"] = (decision.args or {}).get(
                "merchant_policy_status"
            )
            captured["doc_ref"] = (decision.args or {}).get("doc_ref")
            captured["retrieval_count"] = (decision.args or {}).get("retrieval_count")
            captured["response_goal"] = str(getattr(rs, "response_goal", "") or "")
            captured["shipping_knowledge_present"] = "shipping_knowledge" in known
            block = ""
            if rs is not None:
                mc2 = getattr(rs, "merchant_context", None) or {}
                if isinstance(mc2, dict):
                    block = str(mc2.get("retrieved_documents_block") or "")
            if not block:
                mc = getattr(ctx, "merchant_context", None) or {}
                if isinstance(mc, dict):
                    block = str(mc.get("retrieved_documents_block") or "")
            captured["retrieved_documents_block"] = block
            captured["body_in_compose_context"] = "7 أيام" in block
            return (
                "يمكن الاسترجاع خلال 7 أيام من الاستلام بشرط الحالة الأصلية. "
                "تواصل معنا عبر الواتساب للتقديم."
            )

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "core.wa_usage.check_limit",
                    return_value=SimpleNamespace(
                        allowed=True,
                        used_total=0,
                        limit=1000,
                        reason="",
                        pct=0,
                    ),
                )
            )
            # Stub model/composer boundary only — not billing.
            stack.enter_context(
                patch.object(
                    brain._composer,
                    "compose",
                    new_callable=AsyncMock,
                    side_effect=_compose_capture,
                )
            )
            assert has_billing_access(db, tenant.id) is True
            out = _run(
                brain.process(
                    db,
                    tenant.id,
                    _PHONE,
                    "وش سياسة الاسترجاع؟",
                    history=[],
                    profile={"name": "PackA3PresentFixture"},
                    customer_id=None,
                    conversation_id=None,
                )
            )

        assert not out.get("skipped"), out
        assert out.get("reason") != "billing_access_denied"
        reply = str(out.get("reply") or "")
        assert "7 أيام" in reply
        assert captured.get("decision_topic") == "merchant_knowledge_return_policy"
        assert captured.get("policy_surface") == "merchant_knowledge_section"
        assert captured.get("merchant_policy_status") == "KNOWN_PRESENT"
        assert captured.get("retrieval_count", 0) >= 1
        assert str(captured.get("doc_ref") or "").startswith("mks:")
        assert captured.get("body_in_compose_context") is True
        assert "MerchantKnowledgeSection" in (captured.get("response_goal") or "")
        assert captured.get("shipping_knowledge_present") is False

    def test_placeholder_body_excluded_from_customer_retrieval(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents
        from services.merchant_knowledge_customer_readiness import (
            assess_mks_customer_readiness,
        )
        from services.merchant_policy_existence import build_policy_existence_map

        db, _engine = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            title="سياسة الاسترجاع",
            body=_PLACEHOLDER_BODY,
            priority=100,
        )
        verdict = assess_mks_customer_readiness(_PLACEHOLDER_BODY)
        assert verdict.is_ready is False
        assert verdict.status == "INCOMPLETE_AUTHORING_TEMPLATE"
        result = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert len(result.sections) == 0
        assert result.sections_skipped_incomplete >= 1
        existence = build_policy_existence_map(db, tenant.id)
        assert existence["return_policy"]["status"] == "UNKNOWN"
        assert existence["return_policy"]["doc_ref"] is None
