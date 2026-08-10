"""Commerce conversation guard — P0 acceptance scenarios."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.cod_policy_evidence import (  # noqa: E402
    build_cod_policy_reply,
    load_cod_policy_evidence,
)
from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: E402
    catalog_availability_for_name,
    detect_ask_cod,
    filter_catalog_for_active_category,
    is_social_ack_message,
    prepare_commerce_inbound,
    product_title_drifts_from_honey,
    strip_quoted_bot_echo,
)
from modules.ai.brain.types import INTENT_ASK_COD, INTENT_SOCIAL  # noqa: E402


def _product(pid: int, title: str, *, category: str = "", quantity: int = 5) -> dict:
    return {
        "id": pid,
        "title": title,
        "category": category,
        "quantity": quantity,
    }


_HONEY_CATALOG = [
    _product(1, "عسل الطلح البلدي", category="عسل", quantity=10),
    _product(2, "عسل السدر البلدي", category="عسل", quantity=0),
    _product(3, "كريم سم النحل", category="سم النحل", quantity=5),
    _product(4, "زيت سم النحل", category="سم النحل", quantity=5),
    _product(5, "خلية نحل", category="منتجات النحل", quantity=2),
    _product(6, "طلع النخيل", category="منتجات", quantity=2),
]


class TestHoneyCategoryLock:
    def test_honey_offers_lock_category_and_filter_drift(self) -> None:
        prep = prepare_commerce_inbound(
            "عروض العسل إذا أمكن",
            catalog=_HONEY_CATALOG,
        )
        assert prep.session.active_category == "عسل"

        scoped = filter_catalog_for_active_category(
            _HONEY_CATALOG,
            category_scope="عسل",
            message="عروض العسل",
        )
        titles = {p["title"] for p in scoped}
        assert "عسل الطلح البلدي" in titles
        assert "كريم سم النحل" not in titles
        assert "زيت سم النحل" not in titles
        assert "خلية نحل" not in titles
        assert "طلع النخيل" not in titles

    def test_venom_cream_drifts_from_honey_scope(self) -> None:
        assert product_title_drifts_from_honey("كريم سم النحل") is True
        assert product_title_drifts_from_honey("عسل الطلح البلدي") is False


class TestSiderUnavailableConsistency:
    def test_sider_unavailable_from_catalog(self) -> None:
        assert catalog_availability_for_name("سدر", _HONEY_CATALOG) == "unavailable"
        assert catalog_availability_for_name("طلح", _HONEY_CATALOG) == "available"

    def test_sider_ask_marks_unavailable_note(self) -> None:
        prep = prepare_commerce_inbound(
            "ممكن عروض السدر؟",
            catalog=_HONEY_CATALOG,
        )
        assert prep.availability_note == "sider_unavailable"
        assert prep.session.availability_status == "unavailable"


class TestQuotedBotTextIgnored:
    def test_strip_bot_echo_keeps_customer_dua(self) -> None:
        bot = (
            "أبشر يا الغالي، أنا فهمت إنك تبي عسل الطلح.\n"
            "ربع كيلو: 126 ريال — نص كيلo: 240 ريال"
        )
        customer = (
            f"{bot}\n"
            "مبشرة بالخير والجنة"
        )
        addition, stripped = strip_quoted_bot_echo(
            customer,
            [{"direction": "outbound", "body": bot}],
        )
        assert stripped is True
        assert "مبشرة بالخير" in addition
        assert "126 ريال" not in addition

    def test_quoted_plus_dua_is_social_ack_only(self) -> None:
        bot = "أبشر، عسل الطلح متوفر بعدة أحجام."
        msg = f"{bot}\nمبشرة بالخير والجنة"
        prep = prepare_commerce_inbound(
            msg,
            history=[{"direction": "outbound", "body": bot}],
            catalog=_HONEY_CATALOG,
        )
        assert prep.is_social_ack_only is True
        assert prep.intent_override == INTENT_SOCIAL


class TestVariantSelectionCreatesOrderIntent:
    def test_quarter_kilo_talh_sets_order_state(self) -> None:
        state = SimpleNamespace(
            commerce_session={},
            stage="exploring",
            pending_action="",
        )
        prep = prepare_commerce_inbound(
            "أحتاج ربع كيلو من عسل الطلح",
            state=state,
            catalog=_HONEY_CATALOG,
        )
        assert prep.session.order_intent is True
        assert "ربع" in prep.session.active_variant
        assert "طلح" in prep.session.active_product
        assert prep.session.stage == "variant_selected"
        assert state.stage == "ordering"
        assert prep.order_summary_hint


class TestCodQuestion:
    def test_detect_cod_intent(self) -> None:
        msg = "مافي إمكانية تسليم المبلغ وقت استلام الطلب؟"
        assert detect_ask_cod(msg) is True

    def test_cod_prep_overrides_intent(self) -> None:
        prep = prepare_commerce_inbound(msg := (
            "مافي إمكانية تسليم المبلغ وقت استلام الطلب؟"
        ))
        assert prep.is_ask_cod is True
        assert prep.intent_override == INTENT_ASK_COD

    def test_cod_reply_from_settings_when_disabled(self) -> None:
        evidence = load_cod_policy_evidence(
            {"cash_on_delivery_enabled": False},
        )
        reply = build_cod_policy_reply(evidence)
        assert "غير متاح" in reply.reply_text
        # Must not invent runtime bank/wallet defaults.
        assert "الراجحي" not in reply.reply_text
        assert "STC Pay" not in reply.reply_text
        assert evidence.available_methods == []


class TestDuaMustNotBecomeProduct:
    def test_wiak_yarab_is_social_ack(self) -> None:
        assert is_social_ack_message("وياك يارب 💛") is True

    def test_dua_does_not_change_commerce_state(self) -> None:
        state = SimpleNamespace(
            commerce_session={
                "active_category": "عسل",
                "active_product": "عسل الطلح البلدي",
                "active_variant": "ربع كيلo",
                "order_intent": True,
                "stage": "variant_selected",
            },
            stage="ordering",
            pending_action="collect_delivery_info",
        )
        prep = prepare_commerce_inbound("وياك يارب 💛", state=state)
        assert prep.is_social_ack_only is True
        assert prep.session.active_product == "عسل الطلح البلدي"
        assert prep.session.order_intent is True
