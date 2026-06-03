"""Compiler v0 — arrival_contact operational policy."""
from __future__ import annotations

import logging

import pytest

from modules.ai.brain.commerce.arrival_contact_compile_v0 import (
    compile_arrival_contact_policy_v0,
    log_operational_policy_compile,
    verdict_from_compiled_artifact,
)
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


def test_split_policy_and_phone_sections_compile_allow():
    """Policy in escalation_rules + phone in custom → enabled=true."""
    sections = [
        _Section(
            id=10,
            kind="escalation_rules",
            body="عند الوصول للمعرض يُنسّق مع البائع.",
        ),
        _Section(
            id=20,
            kind="custom",
            body="تواصل مع بائع المعرض: 0541690226",
        ),
    ]
    artifact = compile_arrival_contact_policy_v0(sections)
    assert artifact.enabled is True
    assert artifact.action == "send_staff_contact"
    assert artifact.contact_ref == "primary_showroom_seller"
    assert 10 in artifact.source_sections
    assert 20 in artifact.source_sections
    assert artifact.contact_section_id == 20
    assert artifact.contact_lookup_name


def test_metadata_opt_in_plus_phone_section_allow():
    sections = [
        _Section(
            id=11,
            kind="custom",
            body="أمثلة وصول.",
            metadata={
                "intent": "ask_location_or_arrival_help",
                "artifact_target": "maps_link_or_staff_contact",
            },
        ),
        _Section(
            id=21,
            kind="branches",
            body="بائع المعرض — 0555123456",
        ),
    ]
    artifact = compile_arrival_contact_policy_v0(sections)
    assert artifact.enabled is True
    assert artifact.compile_reason == "metadata_opt_in"
    assert 11 in artifact.source_sections
    assert artifact.contact_section_id == 21


def test_staff_phone_only_without_arrival_policy_denies():
    sections = [
        _Section(
            id=1,
            kind="custom",
            body="بائع المعرض: 0541690226 — للاستفسارات العامة.",
        ),
    ]
    artifact = compile_arrival_contact_policy_v0(sections)
    assert artifact.enabled is False
    assert artifact.compile_reason == "no_policy_signal"


def test_arrival_policy_only_without_contact_unresolved():
    sections = [
        _Section(
            id=30,
            kind="escalation_rules",
            body="عند الوصول للمعرض تواصل مع بائع المعرض.",
        ),
    ]
    artifact = compile_arrival_contact_policy_v0(sections)
    assert artifact.enabled is False
    assert artifact.compile_reason == "unresolved_contact"


def test_owner_only_contact_not_used_for_showroom_policy():
    sections = [
        _Section(
            id=40,
            kind="escalation_rules",
            body="عند الوصول للمعرض تواصل مع صاحب المتجر.",
        ),
        _Section(
            id=41,
            kind="owner_identity",
            body="صاحب المتجر: 0555906901",
        ),
    ]
    artifact = compile_arrival_contact_policy_v0(sections)
    assert artifact.enabled is False
    assert artifact.compile_reason == "unresolved_contact"


def test_runtime_prefers_compiled_over_heuristic_on_cross_section():
    sections = [
        _Section(
            id=50,
            kind="escalation_rules",
            body="عند الوصول للمعرض.",
        ),
        _Section(
            id=51,
            kind="custom",
            body="تواصل مع بائع المعرض\n0541690226",
        ),
    ]
    artifact = compile_arrival_contact_policy_v0(sections)
    verdict = verdict_from_compiled_artifact(artifact)
    heuristic = merchant_allows_arrival_staff_contact(sections)

    assert artifact.enabled is True
    assert heuristic.allowed is False
    assert verdict.allowed is True
    assert verdict.policy_source == "compiled_v0"
    assert verdict.reason.startswith("compiled_v0:")


def test_compile_telemetry_lines(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    artifact = compile_arrival_contact_policy_v0([
        _Section(
            id=10,
            kind="escalation_rules",
            body="عند الوصول للمعرض تواصل مع بائع المعرض 0541690226",
        ),
    ])
    log_operational_policy_compile(tenant_id=77, artifact=artifact)
    verdict = verdict_from_compiled_artifact(artifact)
    log_arrival_contact_policy(tenant_id=77, verdict=verdict)

    compile_line = next(
        r.message for r in caplog.records
        if "[OPERATIONAL_POLICY_COMPILE]" in r.message
    )
    policy_line = next(
        r.message for r in caplog.records
        if "[ARRIVAL_CONTACT_POLICY]" in r.message
    )
    assert "tenant=77" in compile_line
    assert "policy=arrival_contact" in compile_line
    assert "enabled=true" in compile_line
    assert "source_sections=" in compile_line
    assert "source=compiled_v0" in policy_line
    assert "allow=true" in policy_line
