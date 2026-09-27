"""The pilot seam's side of the message-intent experiment.

Off by default, and off means no call at all; a tap on a row the platform sent
is never interpreted; the check reads the store's own titles, distinct and
bounded, and says when the list is partial. The provider is a recording double.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import message_intent as mi
from services import commerce_runtime_pilot as seam


class Recording:
    def __init__(self):
        self.calls = []

    def call_single_step(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "ok", "usage": {"input_tokens": 10, "output_tokens": 5},
                "blocks": [{"type": "tool_use", "name": mi.TOOL_NAME,
                            "input": {"decision": mi.NON_COMMERCIAL, "reason": "r"}}]}


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'intent.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("CREATE TABLE tenant_settings (tenant_id INTEGER, store_settings TEXT)"))
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, tenant_id INTEGER, "
                          "title TEXT, in_stock BOOLEAN)"))
        conn.execute(text("INSERT INTO tenants VALUES (1, 'متجر تجريبي عام'), (2, 'متجر آخر')"))
        rows = [(1, "حذاء رياضي أبيض", 1), (1, "حذاء رياضي أبيض", 1), (1, "قميص قطني أزرق", 0),
                (1, "عطر ورد 100ml", 1), (2, "منتج متجر آخر", 1)]
        for tenant, title, stock in rows:
            conn.execute(text("INSERT INTO products (tenant_id, title, in_stock) VALUES (:t, :ti, :s)"),
                         {"t": tenant, "ti": title, "s": stock})
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def run(db, provider, metadata=None):
    return seam._message_intent(db, tenant_id=1, text="نص", history=[],
                                inbound_metadata=metadata or {}, model="configured-test-model",
                                provider=provider)


def test_off_by_default_means_no_call_at_all(db, monkeypatch):
    monkeypatch.delenv(seam.INTENT_CHECK_ENV, raising=False)
    provider = Recording()
    assert run(db, provider) is None and provider.calls == []


@pytest.mark.parametrize("value", ["true", "yes", "0", " ", "2"])
def test_only_exactly_one_turns_it_on(db, monkeypatch, value):
    monkeypatch.setenv(seam.INTENT_CHECK_ENV, value)
    provider = Recording()
    assert run(db, provider) is None and provider.calls == []


def test_a_tap_on_a_row_the_platform_sent_is_state_not_words(db, monkeypatch):
    monkeypatch.setenv(seam.INTENT_CHECK_ENV, "1")
    provider = Recording()
    assert run(db, provider, {"list_reply_id": "nahla:choice:7"}) is None
    assert provider.calls == []


def test_on_it_reads_this_store_s_own_titles_distinct_in_stock_first(db, monkeypatch):
    monkeypatch.setenv(seam.INTENT_CHECK_ENV, "1")
    provider = Recording()
    assessment = run(db, provider)
    assert assessment.decision == mi.NON_COMMERCIAL
    payload = json.loads(provider.calls[0]["messages"][0]["content"])
    assert payload["store"]["name"] == "متجر تجريبي عام"
    assert payload["store"]["product_titles"] == ["حذاء رياضي أبيض", "عطر ورد 100ml", "قميص قطني أزرق"]
    assert payload["store"]["product_titles_complete"] is True
    assert provider.calls[0]["audit_context"]["tenant_id"] == 1


def test_a_catalogue_past_the_bound_is_reported_partial(db, monkeypatch):
    monkeypatch.setenv(seam.INTENT_CHECK_ENV, "1")
    for i in range(mi.MAX_PRODUCT_TITLES + 3):
        db.execute(text("INSERT INTO products (tenant_id, title, in_stock) VALUES (1, :t, 1)"),
                   {"t": f"منتج {i}"})
    db.commit()
    provider = Recording()
    run(db, provider)
    payload = json.loads(provider.calls[0]["messages"][0]["content"])
    assert len(payload["store"]["product_titles"]) == mi.MAX_PRODUCT_TITLES
    assert payload["store"]["product_titles_complete"] is False


def test_an_unreadable_catalogue_gives_the_check_no_titles_not_a_guess(db, monkeypatch):
    monkeypatch.setenv(seam.INTENT_CHECK_ENV, "1")
    db.execute(text("DROP TABLE products"))
    db.commit()
    provider = Recording()
    assessment = run(db, provider)
    payload = json.loads(provider.calls[0]["messages"][0]["content"])
    assert payload["store"]["product_titles"] == [] and payload["store"]["name"] == ""
    assert assessment.decision == mi.NON_COMMERCIAL
