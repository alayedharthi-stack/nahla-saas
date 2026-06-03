"""Tests for KB-policy gate: merchant arrival staff-contact opt-in."""
from __future__ import annotations

import logging

import pytest

from modules.ai.brain.commerce.arrival_contact_policy import (
    log_arrival_contact_policy,
    merchant_allows_arrival_staff_contact,
)


class _Section:
    def __init__(
        self,
        *,
        id: int = 0,
        kind: str = "",
        title: str = "",
        body: str = "",
        metadata: dict | None = None,
    ) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.metadata = metadata or {}


def test_escalation_rules_arrival_plus_contact_action_allows():
    sections = [
        _Section(
            id=149,
            kind="escalation_rules",
            title="وصول العميل",
            body="عند الوصول للمعرض تواصل مع بائع المعرض على الرقم المسجل.",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is True
    assert verdict.reason.startswith("text:escalation_rules")
    assert verdict.section_id == 149


def test_custom_section_arrival_contact_phrase_with_phone_allows():
    sections = [
        _Section(
            id=143,
            kind="custom",
            title="بائع المعرض",
            body="بائع المعرض: 0541690226 — للتواصل عند الوصول.",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is True
    assert verdict.source_kind == "custom"


def test_metadata_arrival_contact_flag_allows():
    sections = [
        _Section(
            id=10,
            kind="custom",
            body="أي نص.",
            metadata={"arrival_contact": True},
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is True
    assert verdict.reason == "metadata:arrival_contact"


def test_metadata_intent_and_artifact_allows():
    sections = [
        _Section(
            id=11,
            kind="custom",
            body="أنا عند البوابة",
            metadata={
                "intent": "ask_location_or_arrival_help",
                "artifact_target": "maps_link_or_staff_contact",
            },
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is True
    assert verdict.reason == "metadata:intent=ask_location_or_arrival_help"


def test_staff_phone_without_arrival_policy_denies():
    sections = [
        _Section(
            id=1,
            kind="custom",
            body="بائع المعرض: 0541690226 — للاستفسارات العامة.",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is False
    assert verdict.reason == "no_arrival_contact_policy"


def test_branch_location_without_arrival_policy_denies():
    sections = [
        _Section(
            id=26,
            kind="branches",
            title="المعرض",
            body="موقع المعرض في الرياض — رابط اللوكيشن https://maps.app.goo.gl/abc",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is False


def test_shipping_only_kb_denies():
    sections = [
        _Section(
            id=151,
            kind="shipping_zones",
            body="نوصل لجميع مناطق المملكة خلال 3-5 أيام.",
        ),
        _Section(
            id=76,
            kind="bank_transfer",
            body="التحويل عبر الراجحي — باركود متاح.",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is False


def test_legacy_settings_escalation_rules_allows():
    verdict = merchant_allows_arrival_staff_contact(
        [],
        settings={
            "escalation_rules": (
                "إذا قال العميل أنا جايكم أرسل رقم الموظف المختص."
            ),
        },
    )
    assert verdict.allowed is True
    assert verdict.reason == "text:legacy_escalation_rules"


def test_if_customer_says_jaykom_send_staff_number_allows():
    sections = [
        _Section(
            kind="escalation_rules",
            body="إذا قال العميل أنا جايكم أرسل رقم الموظف.",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is True


def test_at_door_policy_allows():
    sections = [
        _Section(
            kind="escalation_rules",
            body="عند البوابة → تواصل مع البائع.",
        ),
    ]
    verdict = merchant_allows_arrival_staff_contact(sections)
    assert verdict.allowed is True


def test_log_arrival_contact_policy_emits_line(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="nahla.brain.arrival_contact_policy")
    log_arrival_contact_policy(
        tenant_id=99,
        allowed=True,
        reason="text:escalation_rules",
        source_kind="escalation_rules",
        section_id=149,
    )
    line = next(
        r.message for r in caplog.records if "[ARRIVAL_CONTACT_POLICY]" in r.message
    )
    assert "tenant=99" in line
    assert "source=heuristic" in line
    assert "allow=true" in line
    assert "text:escalation_rules" in line
    assert "section_id=149" in line
