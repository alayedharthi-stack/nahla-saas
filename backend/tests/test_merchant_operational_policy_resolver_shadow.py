"""Shadow tests for merchant operational policy resolver (PR-B1 T1–T5)."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.policy.merchant_operational_policy_resolver import (  # noqa: E402
    ACTION_ASK_PRODUCT,
    ACTION_BROWSE_PRODUCTS,
    ACTION_CATALOG_PROMISE,
    ACTION_ESCALATE,
    ACTION_LLM_COMPOSE,
    ACTION_SEND_CONFIGURED_CONTACT,
    ACTION_SEND_STORE_LOCATION,
    resolve_merchant_operational_policy_hint,
)


def _section(*, kind: str, body: str, section_id: int = 1, title: str = "") -> dict:
    return {
        "id": section_id,
        "kind": kind,
        "title": title,
        "body": body,
        "metadata": {},
    }


class TestMerchantOperationalPolicyResolverShadow:
    def test_t1_showroom_operational_policy(self):
        kb = _section(
            kind="custom",
            body=(
                "إذا العميل يريد زيارة المعرض أرسل الموقع ثم رقم البائع "
                "ثم صعّد حسب المستويات."
            ),
        )
        hint = resolve_merchant_operational_policy_hint(
            db=None,
            tenant_id=99,
            message="أنا بالطائف وأبغى أجيكم",
            sections=[kb],
            has_contact_config=True,
            has_location_config=True,
        )

        assert hint.response_purpose == "showroom_visit"
        assert ACTION_SEND_STORE_LOCATION in hint.allowed_actions
        assert ACTION_SEND_CONFIGURED_CONTACT in hint.allowed_actions
        assert ACTION_ESCALATE in hint.allowed_actions
        assert ACTION_BROWSE_PRODUCTS in hint.forbidden_actions
        assert ACTION_CATALOG_PROMISE in hint.forbidden_actions
        assert ACTION_ASK_PRODUCT in hint.forbidden_actions
        assert hint.showroom_policy_hint is not None
        assert hint.showroom_policy_hint.send_location_first is True
        assert hint.conflict is False
        assert hint.missing_config_reason is None

    def test_t2_contact_request_policy(self):
        kb = _section(
            kind="escalation_rules",
            body="إذا طلب العميل رقم موظف أرسل جهة التواصل المهيأة فقط.",
        )
        hint = resolve_merchant_operational_policy_hint(
            db=None,
            tenant_id=99,
            message="أرسل الأرقام لاهنت",
            sections=[kb],
            has_contact_config=True,
        )

        assert hint.contact_policy_hint is not None
        assert hint.contact_policy_hint.require_configured_only is True
        assert hint.contact_policy_hint.allow_named_staff is False
        assert ACTION_SEND_CONFIGURED_CONTACT in hint.allowed_actions
        assert hint.response_purpose == "contact_request"

    def test_t3_missing_config(self):
        kb = _section(
            kind="custom",
            body="إذا طلب العميل رقم البائع أرسل رقم البائع فوراً.",
        )
        hint = resolve_merchant_operational_policy_hint(
            db=None,
            tenant_id=99,
            message="أرسل الأرقام لاهنت",
            sections=[kb],
            has_contact_config=False,
        )

        assert hint.missing_config_reason == "contact_requested_but_missing_config"
        assert hint.required_action is None
        assert hint.allowed_actions == (ACTION_LLM_COMPOSE,)
        assert hint.contact_policy_hint is not None
        assert hint.contact_policy_hint.require_configured_only is True

    def test_t4_browse_safe(self):
        hint = resolve_merchant_operational_policy_hint(
            db=None,
            tenant_id=99,
            message="وش الأنواع المتوفرة؟",
            sections=[
                _section(
                    kind="custom",
                    body=(
                        "إذا العميل يريد زيارة المعرض أرسل الموقع ثم رقم البائع "
                        "ثم صعّد حسب المستويات."
                    ),
                ),
            ],
        )

        assert hint.response_purpose == "browse_discovery"
        assert hint.showroom_policy_hint is None
        assert hint.contact_policy_hint is None
        assert ACTION_BROWSE_PRODUCTS in hint.allowed_actions
        assert ACTION_BROWSE_PRODUCTS not in hint.forbidden_actions
        assert hint.required_action is None

    def test_t5_conflict(self):
        sections = [
            _section(
                kind="custom",
                section_id=1,
                body="إذا طلب العميل رقم موظف أرسل جهة التواصل المهيأة فقط.",
            ),
            _section(
                kind="faq",
                section_id=2,
                body="إذا طلب العميل رقم موظف اذكر اسم البائع ورقمه مباشرة.",
            ),
        ]
        hint = resolve_merchant_operational_policy_hint(
            db=None,
            tenant_id=99,
            message="أرسل الأرقام لاهنت",
            sections=sections,
            has_contact_config=True,
        )

        assert hint.conflict is True
        assert hint.confidence <= 0.3
        assert hint.required_action is None
        assert hint.allowed_actions == (ACTION_LLM_COMPOSE,)
