"""
Regression tests — unified store URL resolver, contact delivery gate,
and catalog copy policy (Jun 2026).
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

_FORBIDDEN_CATALOG = "تفضّل، اختر من الكتالوج 👇"
_STORE_URL = "https://shop.example.sa"
_MAPS_URL = "https://maps.app.goo.gl/test-branch"


class _Section:
    def __init__(self, *, id: int, kind: str, body: str, title: str = "") -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title
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
    snapshot_url: str = "",
    settings_url: str = "",
    whatsapp_button_url: str = "",
    kb_sections: Optional[List[_Section]] = None,
) -> _StubDB:
    sk_stub = _types.ModuleType("core.store_knowledge")

    def _fake_loader(_db: Any, _tid: int) -> Any:
        class _Loader:
            def store_profile(self) -> Dict[str, str]:
                return {"store_url": snapshot_url} if snapshot_url else {}

        return _Loader()

    sk_stub.StoreKnowledgeLoader = _fake_loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.store_knowledge", sk_stub)

    tenant_stub = _types.ModuleType("core.tenant")
    tenant_stub.DEFAULT_STORE = {"store_url": ""}  # type: ignore[attr-defined]
    tenant_stub.DEFAULT_WHATSAPP = {"store_button_url": ""}  # type: ignore[attr-defined]

    def _fake_settings(_db: Any, _tid: int) -> Any:
        class _Settings:
            store_settings = (
                {"store_url": settings_url} if settings_url else {}
            )
            whatsapp_settings = (
                {"store_button_url": whatsapp_button_url}
                if whatsapp_button_url else {}
            )

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


# ── Store URL tests ──────────────────────────────────────────────────────────


def test_online_store_inquiry_uses_structured_store_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce.store_url_resolver import resolve_store_url
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ONLINE_STORE_INQUIRY
    from modules.ai.brain.compose.templates import faq_store_info

    db = _install_store_resolver_stubs(
        monkeypatch,
        settings_url=_STORE_URL,
    )
    intent = match("عندكم متجر الكتروني ؟")
    assert intent is not None
    assert intent.name == INTENT_ONLINE_STORE_INQUIRY

    resolved = resolve_store_url(db, 1)
    assert resolved.found is True
    assert resolved.url == _STORE_URL
    assert resolved.source == "structured_settings"

    reply = faq_store_info(store_url=resolved.url)
    assert _STORE_URL in reply
    assert "ما عندي رابط" not in reply
    assert "منتج" not in reply.lower()


def test_online_store_inquiry_uses_kb_free_text_store_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce import store_url_resolver as sur

    monkeypatch.setattr(
        sur,
        "_lookup_kb_store_url",
        lambda _db, _tid: (_STORE_URL, "kb_free_text:custom"),
    )
    db = _install_store_resolver_stubs(monkeypatch)
    resolved = sur.resolve_store_url(db, 1)
    assert resolved.found is True
    assert resolved.url == _STORE_URL
    assert resolved.source == "kb_free_text"


def test_online_store_inquiry_does_not_confuse_maps_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce.store_url_resolver import resolve_store_url
    from modules.ai.brain.compose.templates import faq_store_info

    kb_body = f"موقع الفرع على الخريطة: {_MAPS_URL}"
    db = _install_store_resolver_stubs(
        monkeypatch,
        kb_sections=[_Section(id=1, kind="branches", body=kb_body)],
    )
    resolved = resolve_store_url(db, 1)
    assert resolved.found is False
    reply = faq_store_info(store_url=resolved.url)
    assert _MAPS_URL not in reply
    assert "محفوظ في النظام" in reply


def test_online_store_inquiry_missing_url_safe_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce.store_url_resolver import resolve_store_url
    from modules.ai.brain.compose.templates import faq_store_info
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    db = _install_store_resolver_stubs(monkeypatch)
    resolved = resolve_store_url(db, 1)
    assert resolved.found is False

    reply = faq_store_info(store_url="")
    assert "محفوظ في النظام" in reply
    assert "http" not in reply
    assert "اسمك" not in reply
    assert "العنوان" not in reply

    gate = evaluate_contact_delivery_gate(
        customer_message="عندكم متجر الكتروني؟",
    )
    assert gate.allow is False


def test_order_from_website_does_not_trigger_checkout_slots() -> None:
    from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.execution.faq import TOPIC_STORE_INFO
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
        INTENT_ONLINE_STORE_INQUIRY,
    )

    ctx = BrainContext(
        tenant_id=1,
        customer_phone="966500000000",
        message="ابي اطلب من الموقع",
        intent=Intent(name=INTENT_ONLINE_STORE_INQUIRY, confidence=0.96),
        state=MerchantConversationState(),
        facts=CommerceFacts(store_url=_STORE_URL),
        history=[],
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_FAQ_REPLY
    assert decision.args.get("topic") == TOPIC_STORE_INFO
    assert "customer_first_name" not in str(decision.args)


# ── vCard gate tests ─────────────────────────────────────────────────────────


def test_branch_location_does_not_send_vcard() -> None:
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message="وين موقعكم؟",
        delivery_path="branch_trigger_router",
        policy_deliver_contact=True,
    )
    assert gate.allow is False
    assert gate.reason == "branch_location_only"


def test_product_inquiry_does_not_send_vcard() -> None:
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message="وش عندكم من العسل؟",
        intent_name="ask_product",
        delivery_path="staff_contact_policy",
        policy_deliver_contact=True,
    )
    assert gate.allow is False


def test_order_message_does_not_send_vcard() -> None:
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message="أرسلت لكم طلب من مساند",
        delivery_path="staff_contact_policy",
        policy_deliver_contact=True,
    )
    assert gate.allow is False
    assert gate.reason == "general_order_message"


def test_explicit_branch_contact_request_sends_vcard() -> None:
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message="ابي رقم المعرض",
        delivery_path="staff_contact_policy",
        policy_deliver_contact=True,
    )
    assert gate.allow is True
    assert gate.reason == "explicit_contact_request"


def test_llm_mentions_staff_but_gate_blocks_without_explicit_request() -> None:
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message="وش عندكم؟",
        reply_mentions_staff=True,
        delivery_path="call_marker",
    )
    assert gate.allow is False


def test_online_store_inquiry_never_sends_vcard() -> None:
    from modules.ai.brain.commerce.contact_delivery_gate import (
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message="عندكم متجر الكتروني؟",
        delivery_path="staff_contact_policy",
        policy_deliver_contact=True,
    )
    assert gate.allow is False
    assert gate.reason == "online_store_inquiry"


# ── Catalog copy tests ───────────────────────────────────────────────────────


def test_catalog_send_does_not_use_hardcoded_intro() -> None:
    from modules.ai.brain.commerce.catalog_body_policy import (
        FORBIDDEN_CATALOG_INTRO_MARKERS,
        resolve_catalog_body_text,
    )
    from services.whatsapp_platform.catalog_sender import build_catalog_message_payload

    body = resolve_catalog_body_text("", context_reply="")
    payload = build_catalog_message_payload(
        to="966500000000",
        thumbnail_product_retailer_id="sku-1",
        body_text=body,
    )
    text = payload["interactive"]["body"]["text"]
    for marker in FORBIDDEN_CATALOG_INTRO_MARKERS:
        assert marker not in text


def test_catalog_send_no_fixed_emoji_or_pointer() -> None:
    from modules.ai.brain.commerce.catalog_body_policy import (
        has_fixed_catalog_pointer,
        resolve_catalog_body_text,
    )
    from services.whatsapp_platform.catalog_sender import build_catalog_message_payload

    body = resolve_catalog_body_text("المنتجات متاحة في الكتالوج")
    assert has_fixed_catalog_pointer(body) is False
    payload = build_catalog_message_payload(
        to="966500000000",
        thumbnail_product_retailer_id="sku-1",
        body_text=body,
    )
    assert "👇" not in payload["interactive"]["body"]["text"]


def test_purchase_intent_catalog_prompt_is_not_fixed_template() -> None:
    from modules.ai.brain.commerce.catalog_body_policy import (
        is_forbidden_catalog_intro,
        resolve_catalog_body_text,
    )
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ASK_PRODUCT, INTENT_START_ORDER

    browse = match("ايش عندكم")
    assert browse is not None
    assert browse.name == INTENT_ASK_PRODUCT

    order = match("ابي اطلب")
    assert order is not None
    assert order.name == INTENT_START_ORDER

    body = resolve_catalog_body_text("", context_reply="")
    assert is_forbidden_catalog_intro(body) is False
    assert body != _FORBIDDEN_CATALOG
