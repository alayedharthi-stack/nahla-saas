"""AGENT3-D2 — semantic staff-escalation claim truth.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Live Arabic is a test fixture only. It is not a runtime phrase rule.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from typing import Any, List
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import empty_reply_fallback  # noqa: E402
from core.outbound_sanitizer import contains_handoff_promise  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_HANDOFF, ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.postprocess.staff_escalation_semantic_claims import (  # noqa: E402
    StaffEscalationCandidateClaims,
    StaffEscalationTruthCapabilities,
    capabilities_from_execution_data,
    enforce_staff_escalation_semantic_truth,
    unsupported_claims,
)
from modules.ai.brain.postprocess.staff_escalation_semantic_verifier import (  # noqa: E402
    INTERNAL_VERIFIER_SCOPE,
    _INTERNAL_INSTRUCTION,
    parse_staff_escalation_claim_payload,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    reply_contains_escalation_claim,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

LIVE_FALSE_PROMISE = (
    "تمام، وصلت رسالتك. فريق المتجر بيتابع معك هنا في أقرب وقت."
)
TRUTHFUL_QUEUE_ONLY = "تمام، وصلت رسالتك."
FALSE_NOTIFY_CLAIM = "تم إشعار فريق المتجر."
ASSIGNED_CLAIM = "تم تعيين موظف لمتابعة طلبك."
CONTACT_CLAIM = "هذا رقم الموظف للتواصل المباشر."
KNOWN_DETECTOR_PROMISE = "سيتواصل معك الفريق خلال دقائق."


def _run(coro):
    return asyncio.run(coro)


def _ctx(*, tenant_id: int = 33, conversation_id: int = 10107) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000580",
        message="ابي موظف",
        intent=Intent(name="talk_to_human", confidence=0.95),
        state=MerchantConversationState(),
        facts=CommerceFacts(store_name="متجر تجريبي عام"),
        profile={"name": "أحمد سالم", "conversation_id": conversation_id},
        conversation_id=conversation_id,
    )


def _decision() -> Decision:
    return Decision(action=ACTION_HANDOFF, args={}, reason="customer_request")


def _result(**data: Any) -> ActionResult:
    payload = {
        "type": "handoff",
        "escalation_requested": True,
        "escalation_status": "queued",
        "handoff_session_id": 85,
        "handoff_session_created": True,
        "notification_attempted": False,
        "notification_accepted": False,
        "notification_status": "unavailable",
        "verified_contact_available": False,
        "verified_contact_phone": "",
        **data,
    }
    return ActionResult(success=True, data=payload)


def _claims(
    *,
    registered: bool = False,
    queued: bool = False,
    assigned: bool = False,
    notified: bool = False,
    followup: bool = False,
    contact: bool = False,
    valid: bool = True,
    provenance: str = "injected",
) -> StaffEscalationCandidateClaims:
    return StaffEscalationCandidateClaims(
        claims_request_registered=registered,
        claims_queued=queued,
        claims_staff_assigned=assigned,
        claims_staff_notified=notified,
        claims_future_followup=followup,
        claims_contact_delivered=contact,
        valid_parse=valid,
        provenance=provenance,
    )


async def _compose_handoff(
    *,
    first_text: str,
    second_text: str = TRUTHFUL_QUEUE_ONLY,
    result: ActionResult | None = None,
    classify=None,
    ctx: BrainContext | None = None,
    mutate_action: bool = False,
) -> tuple[str, ActionResult, Decision, List[str], List[str]]:
    composer = DefaultComposer()
    decision = _decision()
    action_result = result or _result()
    context = ctx or _ctx()
    llm_texts: List[str] = []
    replies = [first_text, second_text]

    async def _fake_llm(ctx_inner, action_result_inner, *, decision=None):
        from core.outbound_text_policy import mark_compose_llm  # noqa: PLC0415

        mark_compose_llm(action_result_inner)
        text = replies.pop(0) if replies else TRUTHFUL_QUEUE_ONLY
        llm_texts.append(text)
        return text

    classify_calls: List[str] = []

    async def _classify(text: str, capabilities):
        classify_calls.append(text)
        if classify is not None:
            return await classify(text, capabilities)
        return _claims(registered=True, queued=True)

    async def _mutating_impl(decision_inner, result_inner, ctx_inner):
        decision_inner.action = ACTION_LLM_REPLY
        return await _fake_llm(ctx_inner, action_result_inner=result_inner, decision=decision_inner)

    impl_patch = (
        patch.object(composer, "_compose_impl", new=_mutating_impl)
        if mutate_action
        else patch.object(composer, "_llm_compose", new=_fake_llm)
    )
    with impl_patch, patch(
        "modules.ai.brain.postprocess.staff_escalation_semantic_verifier.classify_staff_escalation_claims",
        new=_classify,
    ):
        text = await composer.compose(decision, action_result, context)
    return text, action_result, decision, classify_calls, llm_texts


class TestCapabilityDerivation:
    def test_live_queue_only_session_85_capabilities(self) -> None:
        caps = capabilities_from_execution_data(_result().data)
        assert caps.request_registered is True
        assert caps.queued is True
        assert caps.staff_assigned is False
        assert caps.staff_notified is False
        assert caps.future_followup_committed is False
        assert caps.contact_delivered is False

    def test_status_assigned_does_not_imply_staff_assigned(self) -> None:
        caps = capabilities_from_execution_data(
            {"escalation_status": "assigned", "handoff_session_id": 9}
        )
        assert caps.queued is True
        assert caps.staff_assigned is False
        assert caps.future_followup_committed is False

    def test_notify_does_not_imply_followup_or_assignment(self) -> None:
        caps = capabilities_from_execution_data(
            {
                "escalation_requested": True,
                "escalation_status": "notified",
                "handoff_session_id": 12,
                "notification_accepted": True,
            }
        )
        assert caps.staff_notified is True
        assert caps.staff_assigned is False
        assert caps.future_followup_committed is False

    def test_contact_requires_verified_phone(self) -> None:
        missing_phone = capabilities_from_execution_data(
            {"verified_contact_available": True, "verified_contact_phone": ""}
        )
        present = capabilities_from_execution_data(
            {"verified_contact_available": True, "verified_contact_phone": "966500000001"}
        )
        assert missing_phone.contact_delivered is False
        assert present.contact_delivered is True

    def test_tenant_payloads_do_not_share_capabilities(self) -> None:
        a = capabilities_from_execution_data(
            {"handoff_session_id": 1, "escalation_status": "queued"}
        )
        b = capabilities_from_execution_data(
            {
                "handoff_session_id": 2,
                "escalation_status": "notified",
                "notification_accepted": True,
            }
        )
        assert CLAIM_QUEUED_ONLY(a)
        assert b.staff_notified is True
        assert a.staff_notified is False


def CLAIM_QUEUED_ONLY(caps: StaffEscalationTruthCapabilities) -> bool:
    return (
        caps.queued
        and not caps.staff_notified
        and not caps.staff_assigned
        and not caps.future_followup_committed
        and not caps.contact_delivered
    )


class TestUnsupportedSet:
    def test_future_followup_is_unsupported_on_queue_only(self) -> None:
        caps = capabilities_from_execution_data(_result().data)
        claims = _claims(registered=True, queued=True, followup=True)
        assert "future_followup" in unsupported_claims(claims, caps)

    def test_queue_only_truthful_claims_are_supported(self) -> None:
        caps = capabilities_from_execution_data(_result().data)
        claims = _claims(registered=True, queued=True)
        assert unsupported_claims(claims, caps) == frozenset()


class TestParser:
    def test_valid_json_object(self) -> None:
        parsed = parse_staff_escalation_claim_payload(
            '{"claims_request_registered": true, "claims_queued": true,'
            ' "claims_staff_assigned": false, "claims_staff_notified": false,'
            ' "claims_future_followup": true, "claims_contact_delivered": false,'
            ' "confidence": 0.9}'
        )
        assert parsed.valid_parse is True
        assert parsed.claims_future_followup is True

    def test_invalid_json_fails_closed_parse(self) -> None:
        parsed = parse_staff_escalation_claim_payload("not-json")
        assert parsed.valid_parse is False

    def test_missing_bool_fails_closed_parse(self) -> None:
        parsed = parse_staff_escalation_claim_payload(
            '{"claims_request_registered": true, "claims_queued": true}'
        )
        assert parsed.valid_parse is False

    def test_string_bool_is_invalid_schema(self) -> None:
        parsed = parse_staff_escalation_claim_payload(
            '{"claims_request_registered": "true", "claims_queued": false,'
            ' "claims_staff_assigned": false, "claims_staff_notified": false,'
            ' "claims_future_followup": false, "claims_contact_delivered": false}'
        )
        assert parsed.valid_parse is False


class TestLiveRegression:
    def test_live_arabic_is_blocked_despite_detector_miss(self) -> None:
        assert contains_handoff_promise(LIVE_FALSE_PROMISE) is None
        assert reply_contains_escalation_claim(LIVE_FALSE_PROMISE) is False

        async def classify(text: str, capabilities):
            if text == LIVE_FALSE_PROMISE:
                return _claims(registered=True, queued=True, followup=True)
            return _claims(registered=True, queued=True)

        text, result, _decision, calls, llm_texts = _run(
            _compose_handoff(first_text=LIVE_FALSE_PROMISE, classify=classify)
        )
        assert LIVE_FALSE_PROMISE not in (text or "")
        assert text == TRUTHFUL_QUEUE_ONLY
        assert result.data["staff_escalation_semantic_verify"]["decision"] == "allowed"
        assert result.data["staff_escalation_semantic_verify"]["candidate_attempt"] == 2
        assert "future_followup" in str(result.data.get("compose_facts_overlay") or "")
        assert LIVE_FALSE_PROMISE not in str(result.data.get("compose_facts_overlay") or "")
        assert len(calls) == 2
        assert llm_texts[0] == LIVE_FALSE_PROMISE

    def test_detector_miss_is_not_allow_gate(self) -> None:
        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True, followup=True)

        text, result, *_rest = _run(
            _compose_handoff(
                first_text=LIVE_FALSE_PROMISE,
                second_text=LIVE_FALSE_PROMISE,
                classify=classify,
            )
        )
        assert contains_handoff_promise(LIVE_FALSE_PROMISE) is None
        assert text == empty_reply_fallback()
        assert result.data["staff_escalation_semantic_verify"]["decision"] == "required_fail_closed"
        assert LIVE_FALSE_PROMISE not in text


class TestCapabilityMatrix:
    def test_queue_only_truthful_candidate_passes(self) -> None:
        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True)

        text, result, *_rest = _run(
            _compose_handoff(first_text=TRUTHFUL_QUEUE_ONLY, classify=classify)
        )
        assert text == TRUTHFUL_QUEUE_ONLY
        assert result.data["staff_escalation_semantic_verify"]["decision"] == "allowed"
        assert result.data["staff_escalation_semantic_verify"]["candidate_attempt"] == 1

    def test_false_notify_claim_is_blocked(self) -> None:
        async def classify(text: str, capabilities):
            if text == FALSE_NOTIFY_CLAIM:
                return _claims(registered=True, queued=True, notified=True)
            return _claims(registered=True, queued=True)

        text, result, *_rest = _run(
            _compose_handoff(first_text=FALSE_NOTIFY_CLAIM, classify=classify)
        )
        assert FALSE_NOTIFY_CLAIM not in text
        assert text == TRUTHFUL_QUEUE_ONLY
        overlay = str(result.data.get("compose_facts_overlay") or "")
        assert "unsupported_claims=staff_notified" in overlay

    def test_real_notify_may_claim_notified_not_followup(self) -> None:
        result = _result(
            notification_accepted=True,
            notification_status="accepted",
            escalation_status="notified",
        )

        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True, notified=True)

        text, stamped, *_rest = _run(
            _compose_handoff(
                first_text="وصل إشعار للفريق.",
                classify=classify,
                result=result,
            )
        )
        assert text == "وصل إشعار للفريق."
        assert stamped.data["staff_escalation_semantic_verify"]["decision"] == "allowed"

    def test_notified_does_not_authorize_future_followup(self) -> None:
        result = _result(
            notification_accepted=True,
            notification_status="accepted",
            escalation_status="notified",
        )

        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True, notified=True, followup=True)

        text, stamped, *_rest = _run(
            _compose_handoff(
                first_text=LIVE_FALSE_PROMISE,
                second_text=LIVE_FALSE_PROMISE,
                classify=classify,
                result=result,
            )
        )
        assert text == empty_reply_fallback()
        assert LIVE_FALSE_PROMISE not in text
        assert stamped.data["staff_escalation_semantic_verify"]["decision"] == "required_fail_closed"

    def test_notified_does_not_authorize_assignment(self) -> None:
        result = _result(
            notification_accepted=True,
            notification_status="accepted",
            escalation_status="notified",
        )

        async def classify(text: str, capabilities):
            if text == ASSIGNED_CLAIM:
                return _claims(registered=True, queued=True, notified=True, assigned=True)
            return _claims(registered=True, queued=True, notified=True)

        text, *_rest = _run(
            _compose_handoff(
                first_text=ASSIGNED_CLAIM,
                classify=classify,
                result=result,
            )
        )
        assert ASSIGNED_CLAIM not in text

    def test_contact_delivery_requires_capability(self) -> None:
        async def classify(text: str, capabilities):
            if text == CONTACT_CLAIM:
                return _claims(registered=True, queued=True, contact=True)
            return _claims(registered=True, queued=True)

        blocked, *_rest = _run(
            _compose_handoff(first_text=CONTACT_CLAIM, classify=classify)
        )
        assert CONTACT_CLAIM not in blocked

        allowed_result = _result(
            verified_contact_available=True,
            verified_contact_phone="966500000001",
        )
        allowed, *_rest = _run(
            _compose_handoff(
                first_text=CONTACT_CLAIM,
                classify=classify,
                result=allowed_result,
            )
        )
        assert allowed == CONTACT_CLAIM


class TestRecomposeAndFailClosed:
    def test_second_overclaim_fails_closed_and_is_not_silent(self) -> None:
        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True, followup=True)

        text, result, *_rest = _run(
            _compose_handoff(
                first_text=LIVE_FALSE_PROMISE,
                second_text=LIVE_FALSE_PROMISE,
                classify=classify,
            )
        )
        assert text == empty_reply_fallback()
        assert text.strip()
        assert result.data["compose_source"] == "fallback_deterministic"
        assert result.data["fallback_reason"] == "staff_escalation_semantic_second_overclaim"

    def test_verifier_unavailable_does_not_send_unverified(self) -> None:
        llm_calls = []

        async def classify(text: str, capabilities):
            return _claims(valid=False, provenance="unavailable")

        composer = DefaultComposer()
        result = _result()

        async def _fake_llm(ctx, action_result, *, decision=None):
            llm_calls.append(1)
            return LIVE_FALSE_PROMISE

        with patch.object(composer, "_llm_compose", new=_fake_llm), patch(
            "modules.ai.brain.postprocess.staff_escalation_semantic_verifier.classify_staff_escalation_claims",
            new=classify,
        ):
            text = _run(composer.compose(_decision(), result, _ctx()))
        assert text == empty_reply_fallback()
        assert LIVE_FALSE_PROMISE not in text
        assert len(llm_calls) == 1
        assert result.data["staff_escalation_semantic_verify"]["decision"] == "required_fail_closed"

    def test_invalid_verifier_json_fails_closed(self) -> None:
        parsed = parse_staff_escalation_claim_payload("{")
        assert parsed.valid_parse is False
        text = _run(
            enforce_staff_escalation_semantic_truth(
                text=LIVE_FALSE_PROMISE,
                decision=_decision(),
                result=_result(),
                ctx=_ctx(),
                compose_impl=_unused_compose,
                classify_claims=_invalid_classify,
            )
        )
        assert text == empty_reply_fallback()

    def test_verifier_exception_fails_closed(self) -> None:
        async def boom(text: str, capabilities):
            raise RuntimeError("injected_verifier_failure")

        text = _run(
            enforce_staff_escalation_semantic_truth(
                text=LIVE_FALSE_PROMISE,
                decision=_decision(),
                result=_result(),
                ctx=_ctx(),
                compose_impl=_unused_compose,
                classify_claims=boom,
            )
        )
        assert text == empty_reply_fallback()
        assert LIVE_FALSE_PROMISE not in text

    def test_recompose_max_is_one(self) -> None:
        llm_calls = []

        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True, followup=True)

        composer = DefaultComposer()
        result = _result()

        async def _fake_llm(ctx, action_result, *, decision=None):
            llm_calls.append(str(action_result.data.get("compose_facts_overlay") or ""))
            return LIVE_FALSE_PROMISE

        with patch.object(composer, "_llm_compose", new=_fake_llm), patch(
            "modules.ai.brain.postprocess.staff_escalation_semantic_verifier.classify_staff_escalation_claims",
            new=classify,
        ):
            _run(composer.compose(_decision(), result, _ctx()))
        assert len(llm_calls) == 2
        assert "PREVIOUS_CANDIDATE_VALIDATION" in llm_calls[1]
        assert "فريق المتجر بيتابع" not in llm_calls[1]


async def _unused_compose(decision, result, ctx) -> str:
    raise AssertionError("compose_impl must not run on verifier failure")


async def _invalid_classify(text: str, capabilities) -> StaffEscalationCandidateClaims:
    return parse_staff_escalation_claim_payload("not-json")


class TestComposeHookIsolation:
    def test_non_handoff_behavior_unchanged_and_zero_verifier_calls(self) -> None:
        composer = DefaultComposer()
        decision = Decision(action=ACTION_LLM_REPLY, args={}, reason="catalog")
        result = ActionResult(success=True, data={"type": "llm"})
        calls: List[str] = []

        async def _impl(decision_inner, result_inner, ctx_inner):
            return "catalog-owned wording"

        async def classify(text: str, capabilities):
            calls.append(text)
            raise AssertionError("non-handoff must not classify")

        with patch.object(composer, "_compose_impl", new=_impl), patch(
            "modules.ai.brain.postprocess.staff_escalation_semantic_verifier.classify_staff_escalation_claims",
            new=classify,
        ):
            text = _run(composer.compose(decision, result, _ctx()))
        assert text == "catalog-owned wording"
        assert calls == []
        assert "staff_escalation_semantic_verify" not in result.data

    def test_original_action_still_triggers_verifier_after_internal_mutation(self) -> None:
        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True)

        text, result, decision, calls, _llm = _run(
            _compose_handoff(
                first_text=TRUTHFUL_QUEUE_ONLY,
                classify=classify,
                mutate_action=True,
            )
        )
        assert calls == [TRUTHFUL_QUEUE_ONLY]
        assert text == TRUTHFUL_QUEUE_ONLY
        assert decision.action == ACTION_LLM_REPLY
        assert result.data["staff_escalation_semantic_verify"]["decision"] == "allowed"

    def test_provenance_attaches_after_verified_candidate(self) -> None:
        async def classify(text: str, capabilities):
            return _claims(registered=True, queued=True)

        text, result, *_rest = _run(
            _compose_handoff(first_text=TRUTHFUL_QUEUE_ONLY, classify=classify)
        )
        assert text == TRUTHFUL_QUEUE_ONLY
        policy = result.data.get("outbound_text_policy") or {}
        assert policy.get("decision_action")
        assert "text_source" in policy

    def test_provenance_attaches_after_recompose(self) -> None:
        async def classify(text: str, capabilities):
            if text == LIVE_FALSE_PROMISE:
                return _claims(registered=True, queued=True, followup=True)
            return _claims(registered=True, queued=True)

        text, result, *_rest = _run(
            _compose_handoff(first_text=LIVE_FALSE_PROMISE, classify=classify)
        )
        assert text == TRUTHFUL_QUEUE_ONLY
        policy = result.data.get("outbound_text_policy") or {}
        assert "text_source" in policy
        assert result.data["staff_escalation_semantic_verify"]["candidate_attempt"] == 2


class TestNoPhraseHacksAndScope:
    def test_runtime_modules_do_not_contain_live_phrase(self) -> None:
        from modules.ai.brain.postprocess import staff_escalation_semantic_claims as claims_mod
        from modules.ai.brain.postprocess import staff_escalation_semantic_verifier as verifier_mod

        for module in (claims_mod, verifier_mod, DefaultComposer):
            source = inspect.getsource(module)
            assert "فريق المتجر بيتابع" not in source
            assert "بيتابع معك" not in source

    def test_internal_instruction_is_operational_only(self) -> None:
        assert INTERNAL_VERIFIER_SCOPE == "D2_OPERATIONAL_CLAIM_CLASSIFICATION_ONLY"
        assert "customer intent" in _INTERNAL_INSTRUCTION.lower()
        assert "فريق المتجر بيتابع" not in _INTERNAL_INSTRUCTION
        assert "المتجر" not in _INTERNAL_INSTRUCTION

    def test_legacy_phrase_detector_still_catches_known_promises(self) -> None:
        assert contains_handoff_promise(KNOWN_DETECTOR_PROMISE) is not None
        assert reply_contains_escalation_claim(KNOWN_DETECTOR_PROMISE) is True

    def test_handoff_promise_patterns_were_not_expanded(self) -> None:
        import core.outbound_sanitizer as sanitizer
        from modules.ai.brain.postprocess import staff_escalation_truth_guard as guard

        assert "بيتابع" not in inspect.getsource(sanitizer)
        assert "بيتابع" not in inspect.getsource(guard)

    def test_pipeline_and_routing_files_untouched_in_this_change(self) -> None:
        responder_src = inspect.getsource(DefaultComposer.compose)
        assert "maybe_enforce_staff_escalation_semantic_truth" in responder_src
        assert "pipeline.py" not in responder_src
        routing = os.path.join(
            _BACKEND,
            "modules",
            "ai",
            "brain",
            "commerce",
            "commerce_entry_catalog_delivery.py",
        )
        with open(routing, encoding="utf-8") as handle:
            routing_src = handle.read()
        assert "_CATALOG_TOKEN_RE" in routing_src


class TestNoEventLoopHacks:
    def test_new_modules_do_not_use_asyncio_run_or_nested_loops(self) -> None:
        from modules.ai.brain.postprocess import staff_escalation_semantic_claims as claims_mod
        from modules.ai.brain.postprocess import staff_escalation_semantic_verifier as verifier_mod

        for module in (claims_mod, verifier_mod):
            source = inspect.getsource(module)
            assert "asyncio.run" not in source
            assert "nest_asyncio" not in source
            assert "new_event_loop" not in source
