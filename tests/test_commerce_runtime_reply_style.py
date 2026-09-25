"""The merchant's saved reply language and tone reach the runtime as per-turn data.

Same seam as the assistant name: the settings save handler, the SQL read, the
WhatsApp seam and the provider serialization are real; the runtime ledger and
transport are replaced at their entry and inference is a recording double.
These tests prove what the model is *given*, never what it writes.

The meaning each value carries is the one the platform already defines for the
setting (``tenant_overlay``), so both runtimes read one definition.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from core.tenant import DEFAULT_AI
from models import TenantSettings
from modules.ai.prompts.tenant_overlay import LANGUAGE_MAP, TONE_MAP
from routers import settings as settings_router
from services import commerce_runtime_pilot as seam

from test_commerce_runtime_assistant_identity import run_input, sessions  # noqa: F401  (fixtures)

FREE_TEXT = "- ردودك لا تتجاوز سطرين.\n- الشحن مجاني دائماً."


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
