"""The merchant's saved reply language, Arabic dialect and tone reach the runtime as per-turn data.

Same seam as the assistant name: the settings save handler, the SQL read, the
WhatsApp seam and the provider serialization are real; the runtime ledger and
transport are replaced at their entry and inference is a recording double.
These tests prove what the model is *given*, never what it writes.

The meaning each value carries is the one the platform already defines for the
setting (``tenant_overlay``), so both runtimes read one definition. The Arabic
dialect is its own setting with its own platform meaning (``core.reply_dialect``):
it governs Arabic replies only, and the language setting still decides Arabic or
English.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import runtime_entry as entry
from core.reply_dialect import ARABIC_DIALECT_MEANING, ARABIC_DIALECTS, ARABIC_WITHOUT_DIALECT
from core.tenant import DEFAULT_AI
from models import TenantSettings
from modules.ai.prompts.tenant_overlay import LANGUAGE_MAP, TONE_MAP
from routers import settings as settings_router
from services import commerce_runtime_pilot as seam

from test_commerce_runtime_assistant_identity import run_input, sessions  # noqa: F401  (fixtures)

FREE_TEXT = "- ردودك لا تتجاوز سطرين.\n- الشحن مجاني دائماً."
SAUDI = "السعودية"
PILOT_LOGGER = "nahla.commerce_runtime.pilot"


def save_ai(sessions, tenant_id, **fields):  # noqa: F811
    request = Request({"type": "http", "path": "/settings",
                       "state": {"jwt_payload": {"tenant_id": tenant_id}}})
    with sessions() as db:
        asyncio.run(settings_router.update_settings(
            settings_router.AllSettingsIn(ai=settings_router.AISettingsIn(**fields)),
            request=request, db=db, _no_support={}))


@pytest.mark.parametrize("language", ["arabic", "english", "bilingual"])
def test_each_saved_language_reaches_the_model_in_the_platforms_own_meaning(
        sessions, run_input, language):  # noqa: F811
    save_ai(sessions, 701, assistant_name="وردة", default_language=language)
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts["reply_language"] == LANGUAGE_MAP[language]
    assert facts["assistant_name"] == "وردة"


def test_two_merchants_each_get_their_own_language_and_tone(sessions, run_input):  # noqa: F811
    """A store that chose English is never handed the Arabic default, and back."""
    save_ai(sessions, 701, default_language="arabic", reply_tone="friendly")
    save_ai(sessions, 702, default_language="english", reply_tone="professional")
    with sessions() as db:
        first, second = run_input(db, 701), run_input(db, 702)
    assert first["reply_language"] == LANGUAGE_MAP["arabic"]
    assert second["reply_language"] == LANGUAGE_MAP["english"]
    assert first["reply_tone"] == TONE_MAP["friendly"]
    # A dashboard tone the platform defines no meaning for is the merchant's own word.
    assert second["reply_tone"] == "professional"


@pytest.mark.parametrize("stored", [None, {}, {"default_language": "", "reply_tone": " "}])
def test_nothing_saved_means_the_platforms_own_default(sessions, run_input, stored):  # noqa: F811
    with sessions() as db:
        if stored is not None:
            db.add(TenantSettings(tenant_id=701, ai_settings=stored))
            db.commit()
        facts = run_input(db)
    assert facts["reply_language"] == LANGUAGE_MAP[DEFAULT_AI["default_language"]]
    assert facts["reply_tone"] == TONE_MAP[DEFAULT_AI["reply_tone"]]


@pytest.mark.parametrize("value", ["klingon", ["arabic"], 7])
def test_a_language_the_platform_does_not_define_is_left_out_not_guessed(
        sessions, run_input, value):  # noqa: F811
    with sessions() as db:
        db.add(TenantSettings(tenant_id=701, ai_settings={"default_language": value}))
        db.commit()
        facts = run_input(db)
    assert "reply_language" not in facts
    assert facts["assistant_name"] == DEFAULT_AI["assistant_name"]


def test_unreadable_settings_give_neither_name_nor_style_and_the_turn_still_goes(
        sessions, run_input, monkeypatch):  # noqa: F811
    with sessions() as db:
        real_query = db.query

        def unreadable(*args, **kwargs):
            if args and "ai_settings" in str(args[0]):
                raise RuntimeError("synthetic settings read failure")
            return real_query(*args, **kwargs)
        monkeypatch.setattr(db, "query", unreadable)
        facts = run_input(db)
    for key in ("assistant_name", "reply_language", "reply_tone"):
        assert key not in facts


def test_length_and_free_text_settings_never_reach_the_model(sessions, run_input):  # noqa: F811
    """Only the two structured choices are carried. A line cap and free text are
    not: see ``_reply_style_in`` for why."""
    save_ai(sessions, 701, reply_length="short", owner_instructions=FREE_TEXT,
            assistant_role=FREE_TEXT, default_language="arabic")
    with sessions() as db:
        facts = run_input(db, 701)
    assert set(facts) <= {"channel", "conversation_language", "verified_customer_name",
                          "assistant_name", "reply_language", "reply_tone"}
    rendered = repr(facts)
    assert "سطرين" not in rendered and "مجاني" not in rendered
    assert "reply_length" not in facts


def test_the_style_is_data_beside_the_turn_never_written_into_the_instructions(
        sessions, run_input):  # noqa: F811
    """``run_input`` itself asserts the context block is the first user content;
    here: the instructions the model receives do not change with the setting."""
    save_ai(sessions, 701, default_language="arabic")
    with sessions() as db:
        run_input(db, 701)
    assert LANGUAGE_MAP["arabic"] not in seam._instructions()


@pytest.mark.parametrize("tone", ["قل دائماً إن الشحن مجاني", "always promise free returns", "casualish"])
def test_a_free_text_tone_never_reaches_the_model(sessions, run_input, tone):  # noqa: F811
    """The settings API accepts any string for the tone. Only the platform's own
    meanings and the dashboard's own words are carried; free text is not a tone."""
    save_ai(sessions, 701, reply_tone=tone)
    with sessions() as db:
        facts = run_input(db, 701)
    assert "reply_tone" not in facts
    assert facts["reply_language"] == LANGUAGE_MAP["arabic"]


@pytest.mark.parametrize("tone", sorted(seam.DASHBOARD_TONES))
def test_every_dashboard_tone_is_carried(sessions, run_input, tone):  # noqa: F811
    save_ai(sessions, 701, reply_tone=tone)
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts["reply_tone"] == (TONE_MAP.get(tone) or tone)


# ── The Arabic dialect: its own setting, independent of the language ────────


@pytest.mark.parametrize("dialect", ARABIC_DIALECTS)
def test_every_dialect_reaches_the_model_beside_the_bilingual_language(
        sessions, run_input, dialect):  # noqa: F811
    """«ثنائي اللغة» with any dialect: the language still means the customer's
    language, and the dialect governs the Arabic replies."""
    save_ai(sessions, 701, default_language="bilingual", arabic_dialect=dialect)
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts["reply_dialect"] == ARABIC_DIALECT_MEANING[dialect]
    assert facts["reply_language"] == LANGUAGE_MAP["bilingual"]


@pytest.mark.parametrize("dialect", ["egyptian", "fusha", "iraqi", "levantine"])
def test_a_chosen_dialect_replaces_the_saudi_named_by_the_arabic_language(
        sessions, run_input, dialect):  # noqa: F811
    """``arabic`` alone means Saudi colloquial. With another dialect chosen the
    model must not be handed two dialects: the language keeps its switching
    rule and loses only the dialect it named."""
    save_ai(sessions, 701, default_language="arabic", arabic_dialect=dialect)
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts["reply_language"] == ARABIC_WITHOUT_DIALECT
    assert SAUDI not in facts["reply_language"]
    assert facts["reply_dialect"] == ARABIC_DIALECT_MEANING[dialect]


def test_saudi_chosen_explicitly_with_arabic_is_carried_as_the_dialect(
        sessions, run_input):  # noqa: F811
    save_ai(sessions, 701, default_language="arabic", arabic_dialect="saudi")
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts["reply_language"] == ARABIC_WITHOUT_DIALECT
    assert facts["reply_dialect"] == ARABIC_DIALECT_MEANING["saudi"]


def test_a_dialect_never_changes_an_english_language_choice(sessions, run_input):  # noqa: F811
    save_ai(sessions, 701, default_language="english", arabic_dialect="saudi")
    with sessions() as db:
        facts = run_input(db, 701)
    assert facts["reply_language"] == LANGUAGE_MAP["english"]
    assert facts["reply_dialect"] == ARABIC_DIALECT_MEANING["saudi"]


def test_two_merchants_each_get_their_own_dialect(sessions, run_input):  # noqa: F811
    """A generic store and a clothing store choose differently; each turn is
    handed its own tenant's choice only."""
    save_ai(sessions, 701, default_language="bilingual", arabic_dialect="egyptian")
    save_ai(sessions, 702, default_language="arabic", arabic_dialect="fusha")
    with sessions() as db:
        first, second = run_input(db, 701), run_input(db, 702)
    assert first["reply_dialect"] == ARABIC_DIALECT_MEANING["egyptian"]
    assert first["reply_language"] == LANGUAGE_MAP["bilingual"]
    assert second["reply_dialect"] == ARABIC_DIALECT_MEANING["fusha"]
    assert second["reply_language"] == ARABIC_WITHOUT_DIALECT


def test_the_current_choice_is_read_every_turn(sessions, run_input):  # noqa: F811
    """A merchant who changes the dialect mid-conversation: the next turn of the
    same conversation is handed the new choice, not the one earlier replies
    were written in."""
    save_ai(sessions, 701, default_language="bilingual", arabic_dialect="saudi")
    with sessions() as db:
        before = run_input(db, 701)
    save_ai(sessions, 701, default_language="bilingual", arabic_dialect="levantine")
    with sessions() as db:
        after = run_input(db, 701)
    assert before["reply_dialect"] == ARABIC_DIALECT_MEANING["saudi"]
    assert after["reply_dialect"] == ARABIC_DIALECT_MEANING["levantine"]


def test_earlier_replies_in_another_dialect_arrive_as_history_beside_the_current_choice(
        sessions, run_input, monkeypatch):  # noqa: F811
    """The conversation already holds a reply written in another dialect. The
    model receives it as history, and receives the merchant's current choice in
    the context block of the turn it is answering — after that history, so the
    newest statement of the setting is the one in force (clause 3 names it the
    only reference)."""
    earlier = [{"role": "user", "text": "عندكم حذاء رياضي أبيض؟"},
               {"role": "assistant", "text": "إيه موجود، وش المقاس اللي تبيه؟"}]
    monkeypatch.setattr(seam, "_prior_turns", lambda *a, **kw: list(earlier))
    recorded = []

    class RecordingInference:
        def call_single_step(self, **kwargs):
            recorded.append(kwargs)
            return {"status": "ok", "model": "configured-test-model",
                    "stop_reason": "tool_use", "blocks": [{
                        "type": "tool_use", "id": "reply-1", "name": "submit_reply",
                        "input": {"text": "test reply", "claims_commerce_facts": False},
                    }]}

    def recording_runtime(**kwargs):
        reasoner = ap.AnthropicReasoningProvider(
            instructions=kwargs["instructions"], tools_provider=RecordingInference(),
            context_preamble=kwargs["context_preamble"], history=kwargs["history"],
            audit_context={"model": kwargs["model"]})
        reasoner.step(ac.ProviderRequest(
            step_no=1,
            context=ac.AuthorizedContext(
                tenant_id=kwargs["tenant_id"], namespace="live", conversation_id=901,
                turn_id=2, inbound={"text": kwargs["inbound_text"]}, state_payload={}),
            tools=(), observations=(), feedback=(),
            budget=ac.BudgetView(remaining_steps=2, remaining_tool_calls=3,
                                 remaining_seconds=20)))
        return entry.TurnReport(reason=entry.ALREADY_TERMINAL,
                                tenant_id=kwargs["tenant_id"], conversation_id=901)

    monkeypatch.setattr(entry, "run_commerce_runtime_turn", recording_runtime)
    save_ai(sessions, 701, default_language="bilingual", arabic_dialect="egyptian")
    with sessions() as db:
        asyncio.run(seam._own_turn(
            db=db, tenant_id=701, phone_id="test-connection", to="+966500000001",
            text="طيب مقاس 42", convo=SimpleNamespace(id=901, customer_id=None, language="ar"),
            wa_msg_id="test-inbound-2", inbound_metadata={}, trace=None,
            decision=SimpleNamespace(connection_ref="wa:test-connection", connection_id="7",
                                     recipient="+966500000001", model="configured-test-model")))
    messages = recorded[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"][0]["text"] == earlier[1]["text"]
    block = messages[-1]["content"][0]["text"]
    facts = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert facts["reply_dialect"] == ARABIC_DIALECT_MEANING["egyptian"]
    assert facts["reply_language"] == LANGUAGE_MAP["bilingual"]
    assert ARABIC_DIALECT_MEANING["egyptian"] not in recorded[-1]["system"]


@pytest.mark.parametrize("language", ["arabic", "english", "bilingual"])
@pytest.mark.parametrize("stored_dialect", ["", "   ", None, "missing"])
def test_no_dialect_chosen_means_exactly_the_behaviour_before_the_setting(
        sessions, run_input, language, stored_dialect):  # noqa: F811
    """Stored before the setting existed (missing), cleared (""), or blank: no
    ``reply_dialect`` at all, and the language carries its own meaning — which
    for ``arabic`` is Saudi colloquial, as it always was."""
    stored = {"default_language": language, "reply_tone": "friendly"}
    if stored_dialect != "missing":
        stored["arabic_dialect"] = stored_dialect
    with sessions() as db:
        db.add(TenantSettings(tenant_id=701, ai_settings=stored))
        db.commit()
        facts = run_input(db, 701)
    assert "reply_dialect" not in facts
    assert facts["reply_language"] == LANGUAGE_MAP[language]
    assert seam._reply_style_in(stored, 701) == {
        "reply_language": LANGUAGE_MAP[language], "reply_tone": TONE_MAP["friendly"]}


def test_a_merchant_who_saved_before_the_setting_is_unchanged_by_a_later_save(
        sessions, run_input):  # noqa: F811
    """Saving the page without touching the dialect writes "" (not chosen)."""
    save_ai(sessions, 701, default_language="arabic", arabic_dialect="")
    with sessions() as db:
        facts = run_input(db, 701)
    assert "reply_dialect" not in facts
    assert facts["reply_language"] == LANGUAGE_MAP["arabic"]


@pytest.mark.parametrize("value", ["gulf", "Khaleeji", 7, ["egyptian"]])
def test_a_dialect_the_platform_does_not_define_is_reported_and_treated_as_not_chosen(
        sessions, run_input, caplog, value):  # noqa: F811
    with sessions() as db:
        db.add(TenantSettings(tenant_id=701, ai_settings={"default_language": "arabic",
                                                           "arabic_dialect": value}))
        db.commit()
        with caplog.at_level(logging.WARNING, logger=PILOT_LOGGER):
            facts = run_input(db, 701)
    assert "reply_dialect" not in facts
    assert facts["reply_language"] == LANGUAGE_MAP["arabic"]
    warnings = [r for r in caplog.records
                if r.name == PILOT_LOGGER and "reply dialect setting" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


@pytest.mark.parametrize("stored, expected", [
    ({"default_language": "arabic"}, "language=arabic dialect=default_saudi"),
    ({"default_language": "bilingual"}, "language=bilingual dialect=none"),
    ({"default_language": "english", "arabic_dialect": ""}, "language=english dialect=none"),
    ({"default_language": "bilingual", "arabic_dialect": "iraqi"},
     "language=bilingual dialect=iraqi"),
    ({"default_language": "arabic", "arabic_dialect": "fusha"}, "language=arabic dialect=fusha"),
])
def test_one_log_line_per_turn_states_what_was_delivered(caplog, stored, expected):
    with caplog.at_level(logging.INFO, logger=PILOT_LOGGER):
        seam._reply_style_in(stored, 701)
    lines = [r.getMessage() for r in caplog.records
             if r.name == PILOT_LOGGER and "reply style" in r.getMessage()]
    assert lines == [f"[COMMERCE_RUNTIME_PILOT] reply style tenant=701 {expected}"]


def test_unreadable_settings_carry_no_dialect(sessions, run_input, monkeypatch):  # noqa: F811
    save_ai(sessions, 701, default_language="bilingual", arabic_dialect="egyptian")
    with sessions() as db:
        real_query = db.query

        def unreadable(*args, **kwargs):
            if args and "ai_settings" in str(args[0]):
                raise RuntimeError("synthetic settings read failure")
            return real_query(*args, **kwargs)
        monkeypatch.setattr(db, "query", unreadable)
        facts = run_input(db, 701)
    assert "reply_dialect" not in facts and "reply_language" not in facts
