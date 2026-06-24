"""
Regression tests — online store inquiry body/CTA consistency (Jun 2026).

Ensures compose, safety-net, and CTA layers share one store_url_resolver
truth and that order/size context does not bleed into store-link turns.
"""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_STORE_URL = "https://shop.example.sa"
_NO_URL_CLAIM = "ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً."


class _Section:
    def __init__(self, *, id: int, kind: str, body: str) -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = ""
        self.metadata = {}
        self.metadata_json = {}
        self.updated_at = id


class _StubQuery:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_StubQuery":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_StubQuery":
        return self

    def limit(self, _n: int) -> "_StubQuery":
        return self

    def all(self) -> List[_Section]:
        return self._sections

    def first(self) -> None:
        return None


class _StubDB:
    def __init__(self, sections: Optional[List[_Section]] = None) -> None:
        self._sections = sections or []

    def query(self, _model: Any) -> _StubQuery:
        return _StubQuery(self._sections)


def _install_store_resolver_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings_url: str = _STORE_URL,
    kb_sections: Optional[List[_Section]] = None,
) -> _StubDB:
    sk_stub = _types.ModuleType("core.store_knowledge")

    def _fake_loader(_db: Any, _tid: int) -> Any:
        class _Loader:
            def store_profile(self) -> Dict[str, str]:
                return {}

        return _Loader()

    sk_stub.StoreKnowledgeLoader = _fake_loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.store_knowledge", sk_stub)

    tenant_stub = _types.ModuleType("core.tenant")
    tenant_stub.DEFAULT_STORE = {"store_url": ""}  # type: ignore[attr-defined]
    tenant_stub.DEFAULT_WHATSAPP = {"store_button_url": ""}  # type: ignore[attr-defined]

    def _fake_settings(_db: Any, _tid: int) -> Any:
        class _Settings:
            store_settings = {"store_url": settings_url} if settings_url else {}
            whatsapp_settings = {}

        return _Settings()

    tenant_stub.get_or_create_settings = _fake_settings  # type: ignore[attr-defined]
    tenant_stub.merge_defaults = lambda stored, defaults: {  # type: ignore[attr-defined]
        **dict(defaults or {}),
        **{k: v for k, v in dict(stored or {}).items() if v is not None},
    }
    monkeypatch.setitem(sys.modules, "core.tenant", tenant_stub)

    models_stub = _types.ModuleType("models")

    class _IntegrationColumn:
        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> Any:
            return _types.SimpleNamespace(
                right=_types.SimpleNamespace(value=other),
            )

    class _IntegrationStub:
        tenant_id = _IntegrationColumn("tenant_id")
        provider = _IntegrationColumn("provider")

    class _MerchantKnowledgeSectionStub:
        tenant_id = _IntegrationColumn("tenant_id")
        kind = _IntegrationColumn("kind")

    models_stub.Integration = _IntegrationStub  # type: ignore[attr-defined]
    models_stub.MerchantKnowledgeSection = _MerchantKnowledgeSectionStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "models", models_stub)

    knowledge_stub = _types.ModuleType("core.knowledge")
    knowledge_stub.apply_ai_visible_kb_query_filters = lambda q: q  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.knowledge", knowledge_stub)

    return _StubDB(kb_sections or [])


# ── 1. Compose + safety-net resolver parity ─────────────────────────────


def test_online_store_inquiry_url_found_compose_and_safety_net_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce.store_inquiry_compose_guard import (
        apply_store_url_to_facts,
    )
    from modules.ai.brain.types import CommerceFacts
    from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

    db = _install_store_resolver_stubs(monkeypatch, settings_url=_STORE_URL)
    facts = CommerceFacts()
    apply_store_url_to_facts(facts, db, tenant_id=1)
    assert facts.store_url == _STORE_URL
    assert facts.store_url_resolved is True
    assert facts.store_url_source == "structured_settings"

    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    faq_text = T.faq_store_info(store_url=facts.store_url)
    assert _STORE_URL in faq_text
    assert _NO_URL_CLAIM not in faq_text

    net = apply_store_link_safety_net(
        db,
        tenant_id=1,
        customer_msg="عندكم متجر الكتروني ؟",
        reply_text=faq_text,
    )
    assert not net.fired or net.skipped_reason == "url_already_in_reply"


# ── 2. Stale size context must not appear in store inquiry handling ───────


def test_online_store_inquiry_with_stale_size_context_does_not_bleed_size_question() -> None:
    from modules.ai.brain.commerce.store_inquiry_compose_guard import (
        body_has_order_size_bleed,
        is_store_link_compose_turn,
        strip_store_inquiry_contradictions,
    )
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        INTENT_ONLINE_STORE_INQUIRY,
        MerchantConversationState,
    )
    from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

    assert is_store_link_compose_turn(
        intent_name=INTENT_ONLINE_STORE_INQUIRY,
        customer_message="عندكم متجر الكتروني ؟",
    )

    polluted = (
        f"حاضر، {_NO_URL_CLAIM}\n"
        "وش الحجم تفضّل"
    )
    cleaned, sn, ss = strip_store_inquiry_contradictions(polluted)
    assert sn is True
    assert ss is True
    assert "وش الحجم" not in cleaned
    assert _NO_URL_CLAIM not in cleaned
    assert not body_has_order_size_bleed(cleaned)

    state = MerchantConversationState()
    state.order_prep.product_id = "p1"
    state.order_prep.product_name = "عسل سدر"
    state.current_product_focus = {"title": "عسل سدر", "id": 1}
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000000",
        message="عندكم متجر الكتروني ؟",
        intent=Intent(name=INTENT_ONLINE_STORE_INQUIRY, confidence=0.96),
        state=state,
        facts=CommerceFacts(store_url=_STORE_URL),
        history=[],
    )
    composer = DefaultComposer()
    base = "هذا رابط المتجر الإلكتروني: https://shop.example.sa"
    out = composer._with_follow_up(base, ctx, topic=TOPIC_STORE_INFO)
    assert out == base
    assert "نكمل اختيار" not in out
    assert "وش الحجم" not in out


# ── 3. CTA body semantically matches button ───────────────────────────────


def test_store_link_cta_body_semantically_matches_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.wa_link_buttons import split_text_for_cta_buttons
    from modules.ai.brain.commerce.store_inquiry_compose_guard import (
        body_claims_no_store_url,
        reconcile_store_link_body_when_url_found,
    )

    polluted = f"حاضر، {_NO_URL_CLAIM}\nوش الحجم تفضّل"
    reconciled = reconcile_store_link_body_when_url_found(polluted, _STORE_URL)
    assert not body_claims_no_store_url(reconciled.body)
    assert _STORE_URL in reconciled.body

    msgs = split_text_for_cta_buttons(reconciled.body)
    assert len(msgs) == 1
    assert msgs[0].cta is not None
    assert msgs[0].cta.url == _STORE_URL
    assert not body_claims_no_store_url(msgs[0].body or "")


# ── 4. Safety net must not leave no-url claim when URL exists ────────────


def test_store_link_safety_net_does_not_append_url_to_no_url_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce.store_inquiry_compose_guard import (
        body_claims_no_store_url,
    )
    from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

    _install_store_resolver_stubs(monkeypatch, settings_url=_STORE_URL)
    polluted = f"حاضر، {_NO_URL_CLAIM}\nوش الحجم تفضّل"
    net = apply_store_link_safety_net(
        db=_StubDB(),
        tenant_id=1,
        customer_msg="عندكم متجر الكتروني ؟",
        reply_text=polluted,
    )
    assert net.fired
    assert net.rewrote_reply
    assert str(net.reason).startswith("url_reconciled:")
    assert not body_claims_no_store_url(net.new_reply)
    assert "وش الحجم" not in net.new_reply
    assert _STORE_URL in net.new_reply

    cta = __import__("core.wa_link_buttons", fromlist=["split_text_for_cta_buttons"])
    msg = cta.split_text_for_cta_buttons(net.new_reply)[0]
    assert not body_claims_no_store_url(msg.body or "")


# ── 5. Order resume hint blocked on store_info ───────────────────────────


def test_online_store_inquiry_does_not_resume_order_size_prompt() -> None:
    from modules.ai.brain.commerce.store_inquiry_compose_guard import (
        should_skip_order_resume_hint,
    )
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        INTENT_ONLINE_STORE_INQUIRY,
        MerchantConversationState,
    )
    from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

    assert should_skip_order_resume_hint(
        topic=TOPIC_STORE_INFO,
        intent_name=INTENT_ONLINE_STORE_INQUIRY,
    )

    state = MerchantConversationState()
    state.order_prep.product_id = "99"
    state.order_prep.product_name = "سدر"
    state.order_prep.product_options_meta = [
        {"name": "الحجم", "required": True},
    ]
    state.current_product_focus = {"title": "سدر", "id": 99}
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000000",
        message="عندكم متجر الكتروني ؟",
        intent=Intent(name=INTENT_ONLINE_STORE_INQUIRY, confidence=0.96),
        state=state,
        facts=CommerceFacts(),
        history=[],
    )
    composer = DefaultComposer()
    hint = composer._order_resume_hint(ctx)
    assert "الحجم" in hint
    faq_body = _NO_URL_CLAIM
    combined = composer._with_follow_up(faq_body, ctx, topic=TOPIC_STORE_INFO)
    assert combined == faq_body
    assert "نكمل اختيار" not in combined


# ── 6. Outbound sync stores CTA metadata ─────────────────────────────────


def test_sync_outbound_body_records_cta_metadata() -> None:
    from core.outbound_send_status import sync_outbound_body_to_final

    class _Row:
        id = 42
        body = "old"
        extra_metadata: Dict[str, Any] = {}

    class _DB:
        def begin_nested(self) -> None:
            return None

        def add(self, _row: Any) -> None:
            pass

        def flush(self) -> None:
            pass

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    row = _Row()
    db = _DB()

    import core.outbound_send_status as oss  # noqa: PLC0415

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(oss, "_find_queued_outbound_row", lambda *a, **k: row)
    try:
        sync_outbound_body_to_final(
            db,
            tenant_id=1,
            recipient="966500000000",
            final_body="body after cta",
            reason="post_cta_normalization",
            cta_metadata={
                "body_after_cta": "body after cta",
                "cta_url": _STORE_URL,
                "cta_label": "فتح الرابط",
                "pre_cta_body": f"حاضر، {_NO_URL_CLAIM}",
            },
        )
    finally:
        monkeypatch.undo()

    assert row.body == "body after cta"
    cta = row.extra_metadata.get("cta_delivery") or {}
    assert cta.get("cta_url") == _STORE_URL
    assert cta.get("cta_label") == "فتح الرابط"
