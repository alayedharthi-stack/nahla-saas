"""The merchant's Arabic-dialect setting is saved, validated and read back.

The dialect is independent of the reply language. The settings API accepts
exactly the platform's dialects (``core.reply_dialect``) plus "" for "not
chosen", rejects anything else, and never lets a save that omits the field
overwrite a stored choice. It is stored in the tenant settings' metadata, never
among ``ai_settings``, whose keys the legacy path hands its model wholesale. A
merchant who has never chosen reads back "" — the effective value is then the
language option's own meaning.

The route tests run the real FastAPI router over SQLite (the same seam the
assistant-name tests use); nothing here reaches a provider or WhatsApp.
"""
from __future__ import annotations

import typing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.reply_dialect import (
    ARABIC_DIALECT_MEANING,
    ARABIC_DIALECTS,
    ARABIC_WITHOUT_DIALECT,
    REACH_ALL,
    REACH_NONE,
    REACH_SOME,
    arabic_dialect_reach,
    chosen_arabic_dialect,
)
from core.tenant import DEFAULT_AI, merge_ai_defaults
from models import TenantSettings
from routers import settings as settings_router

from test_commerce_runtime_assistant_identity import sessions  # noqa: F401  (fixture)


# ── The one definition ───────────────────────────────────────────────────────


def test_the_api_literal_is_the_platforms_dialects_plus_not_chosen() -> None:
    accepted = typing.get_args(settings_router.ArabicDialectIn)
    assert len(accepted) == len(ARABIC_DIALECTS) + 1
    assert set(accepted) == set(ARABIC_DIALECTS + ("",))


def test_every_dialect_has_a_meaning_and_nothing_else_does() -> None:
    assert tuple(ARABIC_DIALECT_MEANING) == ARABIC_DIALECTS
    assert all(meaning.strip() for meaning in ARABIC_DIALECT_MEANING.values())


@pytest.mark.parametrize("value, expected", [
    ("egyptian", "egyptian"), ("levantine", "levantine"), ("fusha", "fusha"),
    # Exactly as the API accepts it: what the API would refuse, the runtime
    # does not quietly honour.
    (" Levantine ", None), ("FUSHA", None), ("Egyptian", None),
    ("", None), ("  ", None), (None, None), ("gulf", None), (7, None), (["saudi"], None),
])
def test_only_a_platform_dialect_counts_as_chosen(value, expected) -> None:
    assert chosen_arabic_dialect(value) == expected


# ── Model-level validation ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", ("",) + ARABIC_DIALECTS)
def test_the_settings_model_accepts_each_dialect_and_not_chosen(value) -> None:
    dumped = settings_router.AISettingsIn(arabic_dialect=value).model_dump(exclude_none=True)
    assert dumped["arabic_dialect"] == value


@pytest.mark.parametrize("value", ["gulf", "Saudi", "khaleeji", "arabic", 3])
def test_the_settings_model_rejects_a_value_the_platform_does_not_define(value) -> None:
    with pytest.raises(ValidationError):
        settings_router.AISettingsIn(arabic_dialect=value)


def test_a_save_that_omits_the_field_carries_no_dialect_to_overwrite_with() -> None:
    dumped = settings_router.AISettingsIn(default_language="bilingual").model_dump(
        exclude_none=True)
    assert "arabic_dialect" not in dumped
    assert dumped["default_language"] == "bilingual"


# ── Defaults ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stored", [None, {}, {"default_language": "english"}])
def test_the_legacy_ai_settings_never_carry_the_dialect(stored) -> None:
    """The legacy path hands every ai_settings key to its model and applies no
    dialect; the setting is therefore not among them, even as a default."""
    assert "arabic_dialect" not in DEFAULT_AI
    assert "arabic_dialect" not in merge_ai_defaults(stored)


def test_arabic_without_dialect_is_arabics_own_meaning_minus_the_dialect() -> None:
    """The dialect-free "arabic" text keeps the language option's switching
    rule word for word, so the two cannot drift apart."""
    from modules.ai.prompts.tenant_overlay import LANGUAGE_MAP  # noqa: PLC0415

    switching = LANGUAGE_MAP["arabic"].split(". ", 1)[1]
    assert ARABIC_WITHOUT_DIALECT.endswith(switching)
    assert "السعودية" not in ARABIC_WITHOUT_DIALECT


@pytest.mark.parametrize("env, expected", [
    ({}, REACH_NONE),
    ({"COMMERCE_RUNTIME_PILOT_ENABLED": "true", "COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST": "5"},
     REACH_NONE),
    ({"COMMERCE_RUNTIME_PILOT_ENABLED": "true", "COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST": "701"},
     REACH_SOME),
    ({"COMMERCE_RUNTIME_PILOT_ENABLED": "true", "COMMERCE_RUNTIME_MODE": "global"}, REACH_ALL),
])
def test_the_reach_follows_the_runtimes_own_admission(monkeypatch, env, expected) -> None:
    for name in ("COMMERCE_RUNTIME_PILOT_ENABLED", "COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST",
                 "COMMERCE_RUNTIME_MODE", "COMMERCE_RUNTIME_GLOBAL_TENANT_DENYLIST"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert arabic_dialect_reach(701) == expected


# ── The real route: PUT /settings and GET /settings ─────────────────────────


@pytest.fixture
def client(sessions):  # noqa: F811
    from core.auth import require_not_support_impersonation  # noqa: PLC0415
    from core.database import get_db  # noqa: PLC0415

    app = FastAPI()
    app.include_router(settings_router.router)
    tenant = {"id": 701}

    @app.middleware("http")
    async def authenticated_merchant(request, call_next):
        request.state.jwt_payload = {"tenant_id": tenant["id"]}
        return await call_next(request)

    def db_session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_session
    app.dependency_overrides[require_not_support_impersonation] = lambda: {}
    with TestClient(app) as test_client:
        yield test_client, tenant


def stored_ai(sessions, tenant_id):  # noqa: F811
    with sessions() as db:
        return db.query(TenantSettings.ai_settings).filter(
            TenantSettings.tenant_id == tenant_id).scalar()


def stored_dialect(sessions, tenant_id):  # noqa: F811
    with sessions() as db:
        meta = db.query(TenantSettings.extra_metadata).filter(
            TenantSettings.tenant_id == tenant_id).scalar() or {}
    return (meta.get("reply_style") or {}).get("arabic_dialect", "")


def test_a_saved_dialect_is_stored_and_read_back(client, sessions):  # noqa: F811
    http, _ = client
    response = http.put("/settings", json={"ai": {"default_language": "bilingual",
                                                  "arabic_dialect": "egyptian"}})
    assert response.status_code == 200, response.text
    assert response.json()["ai"]["arabic_dialect"] == "egyptian"
    assert stored_dialect(sessions, 701) == "egyptian"
    assert "arabic_dialect" not in stored_ai(sessions, 701)
    assert stored_ai(sessions, 701)["default_language"] == "bilingual"
    assert http.get("/settings").json()["ai"]["arabic_dialect"] == "egyptian"


def test_a_merchant_who_never_chose_gets_not_chosen_from_the_api(client):
    http, _ = client
    ai = http.get("/settings").json()["ai"]
    assert ai["arabic_dialect"] == ""
    assert ai["default_language"] == DEFAULT_AI["default_language"]


@pytest.mark.parametrize("value", ["gulf", "Egyptian", 5, ["saudi"]])
def test_an_undefined_dialect_is_refused_and_the_stored_choice_kept(
        client, sessions, value):  # noqa: F811
    http, _ = client
    assert http.put("/settings", json={"ai": {"arabic_dialect": "levantine"}}).status_code == 200
    response = http.put("/settings", json={"ai": {"arabic_dialect": value}})
    assert response.status_code == 422
    assert stored_dialect(sessions, 701) == "levantine"


def test_a_save_without_the_field_keeps_the_choice_and_empty_clears_it(
        client, sessions):  # noqa: F811
    http, _ = client
    http.put("/settings", json={"ai": {"arabic_dialect": "fusha"}})
    response = http.put("/settings", json={"ai": {"assistant_name": "وردة",
                                                  "default_language": "english"}})
    assert response.status_code == 200
    assert stored_dialect(sessions, 701) == "fusha"
    assert stored_ai(sessions, 701)["default_language"] == "english"
    assert http.put("/settings", json={"ai": {"arabic_dialect": ""}}).status_code == 200
    assert stored_dialect(sessions, 701) == ""


def test_each_merchant_keeps_its_own_dialect(client, sessions):  # noqa: F811
    """A generic store and a clothing store choose differently; neither save
    touches the other tenant's row."""
    http, tenant = client
    http.put("/settings", json={"ai": {"default_language": "arabic", "arabic_dialect": "iraqi"}})
    tenant["id"] = 702
    http.put("/settings", json={"ai": {"default_language": "bilingual",
                                       "arabic_dialect": "saudi"}})
    assert stored_dialect(sessions, 701) == "iraqi"
    assert stored_dialect(sessions, 702) == "saudi"
    assert http.get("/settings").json()["ai"]["arabic_dialect"] == "saudi"


def test_a_stored_value_the_api_would_refuse_reads_back_as_not_chosen(client, sessions):  # noqa: F811
    """Written by something other than this API, an undefined value is shown as
    not chosen, so the page never sends it back to be refused."""
    http, _ = client
    with sessions() as db:
        db.add(TenantSettings(tenant_id=701, extra_metadata={"reply_style": {"arabic_dialect": "Egyptian"}}))
        db.commit()
    assert http.get("/settings").json()["ai"]["arabic_dialect"] == ""
    assert http.put("/settings", json={"ai": http.get("/settings").json()["ai"]}).status_code == 200


def test_the_settings_say_which_conversations_the_dialect_reaches(client, monkeypatch):
    http, _ = client
    monkeypatch.setenv("COMMERCE_RUNTIME_PILOT_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST", "701")
    monkeypatch.delenv("COMMERCE_RUNTIME_MODE", raising=False)
    assert http.get("/settings").json()["arabic_dialect_reach"] == REACH_SOME
    monkeypatch.setenv("COMMERCE_RUNTIME_PILOT_ENABLED", "false")
    assert http.get("/settings").json()["arabic_dialect_reach"] == REACH_NONE
