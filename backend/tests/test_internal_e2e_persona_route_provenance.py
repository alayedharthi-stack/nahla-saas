"""Signed internal E2E provenance exports for nested persona compose route metadata."""
from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

from modules.ai.brain.persona.fact_bound_composer import (
    COMPOSE_ATTEMPT_PROVIDER_CALL,
    COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED,
)
from modules.ai.compose.reply_metadata_export import (
    PERSONA_ROUTE_PROVENANCE_FIELDS,
    extract_persona_route_provenance,
)
from services.internal_conversational_e2e_contract import (
    EVIDENCE_SCHEMA_VERSION,
    sign_session_evidence,
    verify_session_evidence,
)
from services.internal_conversational_e2e_harness import _provenance_blockers
from services.merchant_brain_turn import PersonaRouteProvenance, _build_provenance


GENERIC_TENANT_ID = 77
EVIDENCE_KEY = "test-evidence-key-not-a-production-secret"


def _persona_compose_event(
    *,
    persona_compose: dict[str, object],
    compose_source: str = "persona_llm",
    llm_candidate_present: bool = True,
    fallback_reason: str = "",
) -> dict[str, object]:
    return {
        "chosen_path": "fact_bound_persona_compose",
        "compose_source": compose_source,
        "llm_candidate_present": llm_candidate_present,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "fallback_reason": fallback_reason,
        "fallback_action_type": "",
        "persona_compose": persona_compose,
    }


def _catalog_route_payload(
    *,
    compose_attempt: str = COMPOSE_ATTEMPT_PROVIDER_CALL,
    route_provider_configured: bool = True,
    llm_candidate_present: bool = True,
) -> dict[str, object]:
    return {
        "surface": "catalog_product_answer",
        "source": "persona_llm",
        "route_provider": "openai_compatible",
        "route_model": "gpt-4o-mini",
        "route_tier": "tiny",
        "route_source": "platform_default",
        "route_provider_configured": route_provider_configured,
        "compose_attempt": compose_attempt,
        "llm_candidate_present": llm_candidate_present,
    }


def _trace() -> SimpleNamespace:
    return SimpleNamespace(
        response_mode="",
        reply_source="",
        fallback_source="",
        chosen_path="",
    )


def test_persona_llm_catalog_path_exports_all_route_fields() -> None:
    event = _persona_compose_event(persona_compose=_catalog_route_payload())
    provenance = _build_provenance(
        brain_result={
            "compose_source": "persona_llm",
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": event["persona_compose"],
        },
        brain_reply_candidate="حذاء رياضي أبيض متوفر",
        reply_text="حذاء رياضي أبيض متوفر",
        brain_persona_compose_event=event,
        trace=_trace(),
    )
    assert provenance.persona_route == PersonaRouteProvenance(
        route_provider="openai_compatible",
        route_model="gpt-4o-mini",
        route_tier="tiny",
        route_source="platform_default",
        route_provider_configured=True,
        compose_attempt=COMPOSE_ATTEMPT_PROVIDER_CALL,
    )
    exported = asdict(provenance)["persona_route"]
    assert tuple(exported.keys()) == PERSONA_ROUTE_PROVENANCE_FIELDS


def test_route_unconfigured_exports_configured_false_and_skipped_attempt() -> None:
    event = _persona_compose_event(
        persona_compose=_catalog_route_payload(
            compose_attempt=COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED,
            route_provider_configured=False,
            llm_candidate_present=False,
        ),
        compose_source="fallback_deterministic",
        llm_candidate_present=False,
        fallback_reason="route_unconfigured",
    )
    provenance = _build_provenance(
        brain_result={
            "compose_source": "fallback_deterministic",
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": event["persona_compose"],
            "fallback_reason": "route_unconfigured",
        },
        brain_reply_candidate="",
        reply_text="عرض محدود",
        brain_persona_compose_event=event,
        trace=_trace(),
    )
    assert provenance.persona_route is not None
    assert provenance.persona_route.route_provider_configured is False
    assert provenance.persona_route.compose_attempt == COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED
    assert provenance.llm_candidate_present is False
    assert provenance.fallback_reason == "route_unconfigured"


def test_timeout_path_retains_selected_route_and_provider_call_with_candidate_false() -> None:
    event = _persona_compose_event(
        persona_compose=_catalog_route_payload(llm_candidate_present=False),
        llm_candidate_present=False,
        fallback_reason="timeout",
    )
    provenance = _build_provenance(
        brain_result={
            "compose_source": "fallback_deterministic",
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": event["persona_compose"],
            "fallback_reason": "timeout",
        },
        brain_reply_candidate="",
        reply_text="عرض محدود",
        brain_persona_compose_event=event,
        trace=_trace(),
    )
    assert provenance.persona_route is not None
    assert provenance.persona_route.compose_attempt == COMPOSE_ATTEMPT_PROVIDER_CALL
    assert provenance.persona_route.route_provider == "openai_compatible"
    assert provenance.llm_candidate_present is False
    assert provenance.fallback_reason == "timeout"


def test_general_llm_path_has_null_persona_route_without_blocker() -> None:
    provenance = _build_provenance(
        brain_result={
            "compose_source": "llm",
            "chosen_path": "llm",
            "response_mode": "llm",
            "llm_candidate_present": True,
        },
        brain_reply_candidate="generic llm candidate",
        reply_text="generic llm candidate",
        brain_persona_compose_event=None,
        trace=_trace(),
    )
    payload = asdict(provenance)
    assert payload["persona_route"] is None
    assert payload["compose_source"] == "llm"
    assert payload["llm_candidate_present"] is True
    assert _provenance_blockers(payload, evaluated_customer_text=True) == []


def test_signed_provenance_is_deterministic_and_bounded() -> None:
    event = _persona_compose_event(persona_compose=_catalog_route_payload())
    provenance = asdict(
        _build_provenance(
            brain_result={
                "compose_source": "persona_llm",
                "chosen_path": "fact_bound_persona_compose",
                "persona_compose": event["persona_compose"],
            },
            brain_reply_candidate="candidate",
            reply_text="candidate",
            brain_persona_compose_event=event,
            trace=_trace(),
        )
    )
    evidence = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "tenant_id": GENERIC_TENANT_ID,
        "provenance": provenance,
        "verdict": "pass",
    }
    signed_a = sign_session_evidence(evidence, key=EVIDENCE_KEY)
    signed_b = sign_session_evidence(evidence, key=EVIDENCE_KEY)
    assert signed_a["integrity"]["signature"] == signed_b["integrity"]["signature"]
    assert verify_session_evidence(signed_a, key=EVIDENCE_KEY) is True

    encoded = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
    assert "prompt" not in encoded.lower()
    assert "api_key" not in encoded.lower()
    assert set(provenance["persona_route"]) == set(PERSONA_ROUTE_PROVENANCE_FIELDS)


def test_extract_persona_route_rejects_partial_or_invalid_nested_metadata() -> None:
    assert extract_persona_route_provenance(None) is None
    assert extract_persona_route_provenance({}) is None
    assert (
        extract_persona_route_provenance(
            {"persona_compose": {"compose_attempt": COMPOSE_ATTEMPT_PROVIDER_CALL}}
        )
        is None
    )
    assert (
        extract_persona_route_provenance(
            {
                "persona_compose": {
                    **_catalog_route_payload(),
                    "route_provider_configured": "false",
                }
            }
        )
        is None
    )


def test_tenant_isolation_uses_generic_merchant_fixture_only() -> None:
    tenant_a = GENERIC_TENANT_ID
    tenant_b = GENERIC_TENANT_ID + 1
    persona_compose = {
        **_catalog_route_payload(),
        "tenant_id": tenant_a,
        "allowlist_result": "enforce",
    }
    event = _persona_compose_event(persona_compose=persona_compose)
    provenance = _build_provenance(
        brain_result={
            "compose_source": "persona_llm",
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": persona_compose,
        },
        brain_reply_candidate="عطر ورد 100ml متوفر",
        reply_text="عطر ورد 100ml متوفر",
        brain_persona_compose_event=event,
        trace=_trace(),
    )
    exported = asdict(provenance)
    encoded = json.dumps(exported)
    assert str(tenant_a) not in encoded
    assert str(tenant_b) not in encoded
    assert exported["persona_route"]["route_source"] == "platform_default"
