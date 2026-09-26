"""A search with more matches than one list shows, paged end to end — on real PostgreSQL.

Every case runs the real ``run_commerce_runtime_turn``: real admission and
ownership, the real agent loop and Anthropic adapter, the real trusted read
context over the merchant's own rows, the real catalogue search, the real
delivery ledger, and the real navigation store at revision ``0113``. Replies are
persisted through the pilot service's own ``_record``, so a later tap is
verified against exactly what production would have stored. Only two things
are doubles: the Anthropic HTTP call and the WhatsApp transport.

The scripted model is deliberately literal: it reads the search result it was
actually shown and names what it saw — which is how these cases prove the model
never saw more than five products, never named a product it was not shown, and
never handled an offset, an order or a token.

What is proven
==============
* one real search with more than ten matches opens page one: nine rows and a
  "More" row, while the model received at most five products;
* "More" continues the same stored order — no search runs, no statement touches
  the catalogue's search predicates — through the final page, every product
  exactly once, in order;
* page boundaries at 10, 11, 23 and above the platform's cap of 50, where the
  last page says the list does not hold every match and offers no "More";
* a catalogue that changes between pages changes what rows say, never which
  products a page holds;
* every refusal — forged, replayed, expired, another conversation's, another
  tenant's — is named to the model and becomes no page, no search, no product
  selection;
* a tap the channel redelivers is the same finished turn: its page is sent once;
* a verified tap on a row the platform composed resolves and becomes a Card;
* a focused answer, a comparison and a narrowed search are never expanded into
  the catalogue, whatever words the model gives;
* whether a list pages never waits on the model's optional words: a list that
  pages and lacks them asks the model once, and when they still do not come —
  or the step fails — the verified reply goes exactly as the model asked;
* a token that cannot be spent with the reply leaves the page as lines and
  mints nothing.

Merchant-agnostic: a general store selling shirts, shoes, perfume, bags and
watches, with generic customers.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import uuid
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import browse as br
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import navigation as nav
from core.commerce_runtime import navigation_models as nm
from core.commerce_runtime import reply_card as rcard
from core.commerce_runtime import reply_choices as rc
from core.commerce_runtime import runtime_entry as entry
from core.commerce_runtime.ledgers import LedgerRepository
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
)

PHONE = "+966500000321"
MODEL = "model-configured-for-this-pilot"
BUTTON = "Options"          # the model's words, in whatever language the customer used
MORE = "See more"

# One distinct search term per catalogue shape, so each case controls its count.
SHIRTS, SHOES, PERFUME, BAGS, WATCHES = "قميص", "حذاء", "عطر", "حقيبة", "ساعة"
COUNTS = {SHIRTS: 23, SHOES: 10, PERFUME: 55, BAGS: 11, WATCHES: 5}
TITLES = {SHIRTS: "قميص قطني أزرق", SHOES: "حذاء رياضي أبيض", PERFUME: "عطر ورد 100ml",
          BAGS: "حقيبة جلد بنية", WATCHES: "ساعة يد فضية"}


# ── Doubles ──────────────────────────────────────────────────────────────────


def _step(blocks: List[Dict[str, Any]], *, stop_reason: str = "tool_use") -> Dict[str, Any]:
    return {"provider": "anthropic", "model": "scripted-model", "status": "ok",
            "stop_reason": stop_reason, "blocks": blocks,
            "usage": {"input_tokens": 100, "output_tokens": 20}, "request_id": "req"}


def _tool_use(call_id: str, name: str, **arguments: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def _reply(text_body: str, *, refs: Sequence[str] = (), commerce: bool = False,
           choices: Optional[Dict[str, Any]] = None, card: Optional[Dict[str, Any]] = None,
           call_id: str = "reply") -> Dict[str, Any]:
    arguments: Dict[str, Any] = {"text": text_body, "evidence_refs": list(refs),
                                 "claims_commerce_facts": commerce}
    if choices is not None:
        arguments["choices"] = choices
    if card is not None:
        arguments["card"] = card
    return {"type": "tool_use", "id": call_id, "name": ap.REPLY_TOOL_NAME, "input": arguments}


def _last_search_result(messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The search result the model was shown, exactly as the adapter serialised it."""
    for message in reversed(list(messages)):
        for block in reversed(list(message.get("content") or [])):
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                payload = json.loads(block["content"])
                if payload.get("tool") == "search_products":
                    return payload
    raise AssertionError("the model was never shown a search result")


class LiteralModel:
    """Stands in for the HTTP call. Names only what the model was actually shown.

    ``browse(query, ...)`` searches, then offers a selector over exactly the
    products the search result carried. ``answer()`` replies with text alone.
    ``each_call`` runs before every step, for a case that changes the world
    while the model is thinking.
    """

    def __init__(self, script: Callable[[int, Sequence[Mapping[str, Any]]], Dict[str, Any]],
                 each_call: Optional[Callable[[], None]] = None) -> None:
        self._script = script
        self._each_call = each_call
        self.calls: List[Dict[str, Any]] = []

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if self._each_call is not None:
            self._each_call()
        return self._script(len(self.calls), kwargs["messages"])

    # what the model was shown -------------------------------------------
    def search_result(self) -> Dict[str, Any]:
        return _last_search_result(self.calls[-1]["messages"])

    def context_block(self) -> str:
        return self.calls[0]["messages"][-1]["content"][0]["text"] \
            if self.calls[0]["messages"][-1]["content"][0]["text"].startswith("<conversation_context>") \
            else next(block["text"] for message in self.calls[0]["messages"]
                      for block in message["content"]
                      if isinstance(block, Mapping) and str(block.get("text", "")).startswith(
                          "<conversation_context>"))

    def declared(self, name: str) -> Mapping[str, Any]:
        return next(tool for tool in self.calls[0]["tools"] if tool["name"] == name)


def browse(query: str, *, pick: Optional[int] = None, more: Optional[str] = MORE,
           button: Optional[str] = BUTTON) -> LiteralModel:
    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("call_search_1", "search_products", query=query)])
        shown = _last_search_result(messages)["result"]["products"]
        ids = [int(p["product_id"]) for p in shown][:pick]
        choices: Dict[str, Any] = {"product_ids": ids}
        if button is not None:
            choices["button"] = button
        if more is not None:
            choices["more_label"] = more
        return _step([_reply("These are some of the options.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in ids],
                             choices=choices)])
    return LiteralModel(script)


def answer(text_body: str = "Here you go.", **extra: Any) -> LiteralModel:
    return LiteralModel(lambda _call, _messages: _step([_reply(text_body, **extra)]))


@dataclasses.dataclass
class Transport:
    """Accepts every send and remembers it, like a provider that said yes."""

    sent: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def __call__(self, payload: Mapping[str, Any]) -> lc.SendResponse:
        self.sent.append(dict(payload))
        return lc.SendResponse(http_status=200,
                               body={"messages": [{"id": f"wamid.{uuid.uuid4().hex}"}]})


class _Trace:
    def mark_outbound_sent(self, **_kw: Any) -> None:
        return None


# ── Harness ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Shop:
    engine: Any
    session_factory: Any
    tenant_a: int
    tenant_b: int
    connection_a: int
    connection_b: int
    customer_a: int
    customer_b: int
    products: Dict[str, List[int]]           # tenant A's products per search term, in id order

    def conversation(self, *, tenant: Optional[int] = None) -> int:
        tenant = tenant or self.tenant_a
        customer = self.customer_a if tenant == self.tenant_a else self.customer_b
        with self.engine.begin() as conn:
            return int(conn.execute(text(
                "INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                "VALUES (:t, :c, :e, 'active') RETURNING id"),
                {"t": tenant, "c": customer, "e": PHONE}).scalar_one())

    def turn(self, conversation: int, model: LiteralModel, *, question: str = "Show me",
             metadata: Optional[Dict[str, Any]] = None, tenant: Optional[int] = None,
             statements: Optional[List[str]] = None,
             max_steps: int = 3, message_id: Optional[str] = None,
             expect_sent: bool = True) -> Tuple[entry.TurnReport, Transport]:
        """One real turn, then its reply persisted the way the pilot persists it."""
        tenant = tenant or self.tenant_a
        connection = self.connection_a if tenant == self.tenant_a else self.connection_b
        customer = self.customer_a if tenant == self.tenant_a else self.customer_b
        transport = Transport()
        with _capture(self.engine, statements):
            report = entry.run_commerce_runtime_turn(
                engine=self.engine, session_factory=self.session_factory, tenant_id=tenant,
                conversation_id=conversation, connection_ref=f"wa:{connection}",
                connection_id=str(connection), customer_id=customer,
                normalized_customer_phone=PHONE,
                provider_message_id=message_id or "wamid.in." + uuid.uuid4().hex,
                inbound_text=question, inbound_metadata=dict(metadata or {}),
                transport=transport, instructions="EXISTING-INSTRUCTIONS", model=MODEL,
                budget=ac.LoopBudget(max_steps=max_steps, max_tool_calls=4, tool_timeout_seconds=10.0,
                                     provider_timeout_seconds=15.0, deadline_seconds=45.0),
                context_preamble={"channel": "whatsapp"}, anthropic_provider=model)
        if not expect_sent:
            return report, transport
        assert report.dispatch_status == dd.SENT_ACCEPTED, report
        self._record(conversation, tenant, report, transport)
        return report, transport

    def _record(self, conversation: int, tenant: int, report: entry.TurnReport,
                transport: Transport) -> None:
        from services import commerce_runtime_pilot as pilot

        wire = pilot.WireObservation()
        rows, _button = rc.payload_rows(transport.sent[-1])
        wire.record(str(transport.sent[-1].get("text") or ""), [], duplicate_suppressed=False,
                    row_ids=[str(row["id"]) for row in rows])
        db = self.session_factory()
        try:
            convo = db.execute(text("SELECT id FROM conversations WHERE id = :c"),
                               {"c": conversation}).first()
            pilot._record(db=db, trace=_Trace(), convo=convo, tenant_id=tenant, to=PHONE,
                          report=report, wire=wire)
            db.commit()
        finally:
            db.close()

    def token_row(self, token: str) -> Dict[str, Any]:
        with self.engine.connect() as conn:
            row = conn.execute(text(f"SELECT * FROM {nm.NAVIGATION_TABLE} WHERE token = :t"),
                               {"t": token}).mappings().first()
        return dict(row) if row else {}

    def tokens_for(self, conversation: int) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                f"SELECT count(*) FROM {nm.NAVIGATION_TABLE} n "
                "JOIN commerce_runtime_conversations c ON c.id = n.conversation_id "
                "WHERE c.conversation_ref = :r"),
                {"r": f"wa:conversation:{conversation}"}).scalar())


@contextlib.contextmanager
def _capture(engine: Any, statements: Optional[List[str]]) -> Iterator[None]:
    if statements is None:
        yield
        return

    def listen(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listen)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", listen)


def _seed(engine: Any) -> Shop:
    with engine.begin() as conn:
        def tenant(label: str) -> int:
            return int(conn.execute(text(
                "INSERT INTO tenants (name, is_active, is_platform_tenant) "
                "VALUES (:n, true, false) RETURNING id"),
                {"n": f"متجر تجريبي عام {label} {uuid.uuid4().hex[:8]}"}).scalar_one())

        tenant_a, tenant_b = tenant("أ"), tenant("ب")

        def connection(tenant_id: int, number: str) -> int:
            return int(conn.execute(text(
                "INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                "VALUES (:t, :p, 'connected') RETURNING id"), {"t": tenant_id, "p": number}
            ).scalar_one())

        def customer(tenant_id: int, name: str) -> int:
            return int(conn.execute(text(
                "INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                "VALUES (:t, :p, :p, :n) RETURNING id"), {"t": tenant_id, "p": PHONE, "n": name}
            ).scalar_one())

        connection_a, connection_b = connection(tenant_a, "1555000321"), connection(tenant_b, "1555000322")
        customer_a, customer_b = customer(tenant_a, "نورة عبدالله"), customer(tenant_b, "أحمد سالم")

        def product(tenant_id: int, title: str, price: int) -> int:
            ref = uuid.uuid4().hex[:10]
            return int(conn.execute(text(
                "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
                "stock_quantity, metadata) VALUES (:t, :x, :ti, :d, :p, true, 5, CAST(:m AS JSONB)) "
                "RETURNING id"),
                {"t": tenant_id, "x": "SKU-" + ref, "ti": title, "d": "منتج عام", "p": price,
                 "m": json.dumps({"image_url": f"https://cdn.example.test/{ref}.jpg",
                                  "product_url": f"https://shop.example.test/p/{ref}"})}
            ).scalar_one())

        products: Dict[str, List[int]] = {}
        for term, count in COUNTS.items():
            # Interleave the other tenant's identical products so ids are not a
            # contiguous run a test could accidentally lean on.
            ids = []
            for n in range(count):
                ids.append(product(tenant_a, f"{TITLES[term]} {n + 1}", 100 + n))
                if n % 4 == 0:
                    product(tenant_b, f"{TITLES[term]} {n + 1}", 100 + n)
            products[term] = ids
    return Shop(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False),
                tenant_a=tenant_a, tenant_b=tenant_b, connection_a=connection_a,
                connection_b=connection_b, customer_a=customer_a, customer_b=customer_b,
                products=products)


@pytest.fixture(scope="module")
def shop(pg_admin_dsn: str) -> Iterator[Shop]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, "0111")
        _alembic(dsn, "0113")
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        yield _seed(engine)
    finally:
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _rows(payload: Mapping[str, Any]) -> Tuple[List[int], Optional[Dict[str, Any]], str]:
    """Product ids, the "More" row if any, and the button, as the wire received them."""
    rows, button = rc.payload_rows(payload)
    products = [rc.product_id_from_row_id(row["id"]) for row in rows
                if rc.product_id_from_row_id(row["id"]) is not None]
    more = [row for row in rows if nav.token_from_row_id(row["id"]) is not None]
    assert len(more) <= 1
    return products, (more[0] if more else None), button


def _tap_more(shop: Shop, conversation: int, more_row: Mapping[str, Any], *,
              statements: Optional[List[str]] = None,
              model: Optional[LiteralModel] = None, message_id: Optional[str] = None,
              expect_sent: bool = True) -> Tuple[entry.TurnReport, Transport, LiteralModel]:
    model = model or answer()
    report, transport = shop.turn(
        conversation, model, question=str(more_row["title"]), statements=statements,
        metadata={"list_reply_id": more_row["id"], "list_reply_title": more_row["title"]},
        message_id=message_id, expect_sent=expect_sent)
    return report, transport, model


def _searched(statements: Sequence[str]) -> List[str]:
    """Statements that ran a catalogue search predicate."""
    return [s for s in statements
            if "to_tsvector" in s or "plainto_tsquery" in s
            or ("products.title" in s.lower() and "like" in s.lower())]


def _facts(model: LiteralModel) -> Dict[str, Any]:
    block = model.context_block()
    body = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
    return body


# ── Page one ─────────────────────────────────────────────────────────────────


def test_one_search_with_more_than_ten_matches_opens_page_one(shop: Shop):
    conversation = shop.conversation()
    model = browse(SHIRTS)
    report, transport = shop.turn(conversation, model)
    ids = shop.products[SHIRTS]

    # The model saw a bounded window, and was told only that more exist.
    shown = model.search_result()["result"]
    assert len(shown["products"]) <= 5
    assert shown["more_results"] is True
    everything_the_model_read = json.dumps([call["messages"] for call in model.calls])
    for hidden in ids[5:]:
        assert f'"product_id": {hidden}' not in everything_the_model_read
    assert "more_label" in model.declared(ap.REPLY_TOOL_NAME)["input_schema"]["properties"][
        "choices"]["properties"]

    products, more, button = _rows(transport.sent[0])
    assert products == ids[:9], "page one is the search's head, in its order"
    assert more is not None and more["title"] == MORE and button == BUTTON
    assert report.browse_outcome == br.OPENED and report.choice_rows == 9
    assert report.navigation_page == 1 and report.navigation_has_next is True
    stored = shop.token_row(nav.token_from_row_id(more["id"]))
    assert stored["product_ids"] == ids and stored["complete"] is True
    assert stored["page_offset"] == 9 and stored["consumed_at"] is None
    assert stored["minted_by_turn_id"] == report.turn_id == stored["origin_turn_id"]
    assert stored["origin_call_id"] == "call_search_1"
    assert stored["more_label"] == MORE and stored["button_label"] == BUTTON
    # The model's text is carried untouched; the rows are the platform's.
    assert transport.sent[0]["text"] == "These are some of the options."


def test_more_continues_the_same_snapshot_without_another_search(shop: Shop):
    conversation = shop.conversation()
    ids = shop.products[SHIRTS]
    _report, first = shop.turn(conversation, browse(SHIRTS))
    _products, more, _button = _rows(first.sent[0])
    seen: List[int] = list(_products)
    pages = 1
    tokens: List[str] = []
    while more is not None:
        statements: List[str] = []
        tokens.append(nav.token_from_row_id(more["id"]))
        report, transport, model = _tap_more(shop, conversation, more, statements=statements)
        pages += 1
        assert _searched(statements) == [], "a page must never search again"
        assert report.tools_called == (), "the model was asked for nothing"
        facts = _facts(model)[br.FACTS_KEY]
        assert facts["status"] == nav.RESOLVED and facts["page_number"] == pages
        products, more, button = _rows(transport.sent[0])
        assert button == BUTTON, "later pages carry the model's own words"
        assert facts["products_on_this_page"] == len(products)
        seen.extend(products)
        assert report.browse_outcome == br.PAGE
        # The tapped token was spent by exactly this turn's reply.
        assert shop.token_row(tokens[-1])["consumed_by_turn_id"] == report.turn_id
    assert seen == ids, "every match exactly once, in the order the search found them"
    assert pages == 3


@pytest.mark.parametrize("term,pages,sizes", [
    (SHOES, 1, [10]),          # ten fit one list: nothing is stored
    (BAGS, 2, [9, 2]),         # eleven: the first boundary
    (SHIRTS, 3, [9, 9, 5]),
    (PERFUME, 6, [9, 9, 9, 9, 9, 5]),   # fifty-five: above the cap of fifty
])
def test_page_boundaries_and_the_cap(shop: Shop, term, pages, sizes):
    conversation = shop.conversation()
    ids = shop.products[term]
    before = shop.tokens_for(conversation)
    report, transport = shop.turn(conversation, browse(term))
    products, more, _button = _rows(transport.sent[0])
    walked = [products]
    last_model: Optional[LiteralModel] = None
    while more is not None:
        _report, transport, last_model = _tap_more(shop, conversation, more)
        products, more, _button = _rows(transport.sent[0])
        walked.append(products)
    assert [len(page) for page in walked] == sizes
    flat = [pid for page in walked for pid in page]
    assert flat == ids[:50] and len(set(flat)) == len(flat)
    if term == SHOES:
        assert report.browse_outcome == br.WHOLE and shop.tokens_for(conversation) == before
    if term == PERFUME:
        # Fifty stored of fifty-five: the last page says so and offers no "More".
        facts = _facts(last_model)[br.FACTS_KEY]
        assert facts["more_matches_than_the_list_holds"] is True
        assert facts["more_pages_after_this"] is False
        assert shop.products[PERFUME][50] not in flat


def test_a_search_the_model_saw_whole_is_not_expanded(shop: Shop):
    conversation = shop.conversation()
    model = browse(WATCHES)
    report, transport = shop.turn(conversation, model)
    assert model.search_result()["result"]["more_results"] is False
    products, more, _button = _rows(transport.sent[0])
    assert products == shop.products[WATCHES] and more is None
    # Declined, and saying why: the reply offers exactly what the model named.
    assert report.browse_outcome == br.NOTHING_MORE and report.choices_outcome == rc.OFFERED


# ── The catalogue changes between pages ──────────────────────────────────────


def test_a_catalogue_that_moves_changes_what_rows_say_never_which_products_a_page_holds(shop: Shop):
    conversation = shop.conversation()
    ids = shop.products[SHIRTS]
    _report, first = shop.turn(conversation, browse(SHIRTS))
    _products, more, _button = _rows(first.sent[0])
    removed, repriced = ids[10], ids[12]
    with shop.engine.begin() as conn:
        conn.execute(text("UPDATE products SET tenant_id = :b WHERE id = :p"),
                     {"b": shop.tenant_b, "p": removed})       # gone from this merchant
        conn.execute(text("UPDATE products SET price = 777 WHERE id = :p"), {"p": repriced})
        newcomer = int(conn.execute(text(
            "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
            "stock_quantity) VALUES (:t, 'SKU-NEW', 'قميص قطني جديد', 'جديد', 50, true, 5) "
            "RETURNING id"), {"t": shop.tenant_a}).scalar_one())
    try:
        report, transport, model = _tap_more(shop, conversation, more)
        products, more, _button = _rows(transport.sent[0])
        assert products == [pid for pid in ids[9:18] if pid != removed]
        assert newcomer not in products, "a page never grows a product the search did not find"
        facts = _facts(model)[br.FACTS_KEY]
        assert facts["no_longer_available"] == 1 and facts["products_on_this_page"] == 8
        repriced_row = next(row for row in rc.payload_rows(transport.sent[0])[0]
                            if row["id"] == rc.row_id(repriced))
        assert "777" in repriced_row.get("description", "") + repriced_row["title"]
        navigation = transport.sent[0][rc.CHOICES_KEY][rc.NAVIGATION_KEY]
        assert navigation["unavailable"] == 1 and navigation["page"] == 2
        _report, transport, _model = _tap_more(shop, conversation, more)
        assert _rows(transport.sent[0])[0] == ids[18:23], "the next page did not move"
    finally:
        with shop.engine.begin() as conn:
            conn.execute(text("UPDATE products SET tenant_id = :a WHERE id = :p"),
                         {"a": shop.tenant_a, "p": removed})
            conn.execute(text("UPDATE products SET price = 112 WHERE id = :p"), {"p": repriced})
            conn.execute(text("DELETE FROM products WHERE id = :p"), {"p": newcomer})


# ── Refusals ─────────────────────────────────────────────────────────────────


def _assert_refused(report: entry.TurnReport, transport: Transport, model: LiteralModel,
                    statements: Sequence[str], status: str) -> None:
    assert report.navigation_tap == status
    assert _facts(model)[br.FACTS_KEY] == {"navigation": "next_page_of_an_earlier_list",
                                           "status": status}
    assert "customer_tapped" not in model.context_block()
    assert _searched(statements) == [] and report.tools_called == ()
    assert transport.sent[0].get(rc.CHOICES_KEY) is None
    assert report.delivery_kind == lc.DeliveryKind.TEXT.value


def test_a_forged_token_opens_nothing(shop: Shop):
    conversation = shop.conversation()
    statements: List[str] = []
    report, transport, model = _tap_more(
        shop, conversation, {"id": nav.row_id(nav.new_token()), "title": MORE},
        statements=statements)
    _assert_refused(report, transport, model, statements, nav.NOT_FOUND)


def test_a_replayed_token_opens_nothing(shop: Shop):
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(SHIRTS))
    _products, more, _button = _rows(first.sent[0])
    _tap_more(shop, conversation, more)
    statements: List[str] = []
    report, transport, model = _tap_more(shop, conversation, more, statements=statements)
    _assert_refused(report, transport, model, statements, nav.REPLAYED)


def test_an_expired_token_opens_nothing(shop: Shop):
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(BAGS))
    _products, more, _button = _rows(first.sent[0])
    with shop.engine.begin() as conn:
        conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET expires_at = now() - interval "
                          "'1 minute' WHERE token = :t"), {"t": nav.token_from_row_id(more["id"])})
    statements: List[str] = []
    report, transport, model = _tap_more(shop, conversation, more, statements=statements)
    _assert_refused(report, transport, model, statements, nav.EXPIRED)
    assert shop.token_row(nav.token_from_row_id(more["id"]))["consumed_at"] is None


def test_a_redelivered_more_tap_sends_its_page_once(shop: Shop):
    """The channel delivers the same tap twice. The second is the same turn,
    already finished: nothing is sent, the model is not asked, and no token is
    spent or minted by it."""
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(SHIRTS))
    _products, more, _button = _rows(first.sent[0])
    token = nav.token_from_row_id(more["id"])
    message_id = "wamid.in.redelivered." + uuid.uuid4().hex
    report, transport, _model = _tap_more(shop, conversation, more, message_id=message_id)
    page_two, next_more, _button = _rows(transport.sent[0])
    assert page_two == shop.products[SHIRTS][9:18] and next_more is not None
    rows_after_page = shop.tokens_for(conversation)

    model = answer()
    again, resent, _model = _tap_more(shop, conversation, more, model=model, message_id=message_id,
                                      expect_sent=False)
    assert again.duplicate_inbound is True and again.turn_id == report.turn_id
    assert again.reason == entry.ALREADY_TERMINAL
    assert resent.sent == [] and model.calls == []
    assert shop.tokens_for(conversation) == rows_after_page, "nothing minted twice"
    assert shop.token_row(token)["consumed_by_turn_id"] == report.turn_id
    assert shop.token_row(nav.token_from_row_id(next_more["id"]))["consumed_at"] is None


def test_another_conversations_token_opens_nothing_here_and_stays_theirs(shop: Shop):
    theirs, ours = shop.conversation(), shop.conversation()
    _report, first = shop.turn(theirs, browse(BAGS))
    _products, more, _button = _rows(first.sent[0])
    statements: List[str] = []
    report, transport, model = _tap_more(shop, ours, more, statements=statements)
    _assert_refused(report, transport, model, statements, nav.NOT_FOUND)
    _report, transport, _model = _tap_more(shop, theirs, more)
    assert _rows(transport.sent[0])[0] == shop.products[BAGS][9:11]


def test_another_tenants_token_opens_nothing_here(shop: Shop):
    ours = shop.conversation()
    _report, first = shop.turn(ours, browse(BAGS))
    _products, more, _button = _rows(first.sent[0])
    theirs = shop.conversation(tenant=shop.tenant_b)
    statements: List[str] = []
    model = answer()
    report, transport = shop.turn(
        theirs, model, tenant=shop.tenant_b, statements=statements, question=MORE,
        metadata={"list_reply_id": more["id"], "list_reply_title": MORE})
    _assert_refused(report, transport, model, statements, nav.NOT_FOUND)
    assert shop.token_row(nav.token_from_row_id(more["id"]))["consumed_at"] is None


def test_a_product_row_id_is_never_read_as_navigation_and_the_reverse(shop: Shop):
    assert nav.token_from_row_id(rc.row_id(shop.products[SHIRTS][0])) is None
    assert rc.product_id_from_row_id(nav.row_id(nav.new_token())) is None


# ── Taps, focus and precedence ───────────────────────────────────────────────


def test_a_tap_on_a_row_the_platform_composed_becomes_a_card(shop: Shop):
    """Row seven of page one was never shown to the model and never cited.

    It was sent, so it verifies — re-read now — and the platform's own read
    makes the Card, with the model's button word.
    """
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(SHIRTS))
    products, _more, _button = _rows(first.sent[0])
    seventh = products[6]
    model = answer("Here it is.", card={"button_label": "View"})
    report, transport = shop.turn(
        conversation, model, question="row seven",
        metadata={"list_reply_id": rc.row_id(seventh), "list_reply_title": "row seven"})
    assert f'"product_id": {seventh}' in model.context_block()
    card = rcard.payload_card(transport.sent[0])
    assert card is not None and card["product_id"] == seventh and card["button_label"] == "View"
    assert report.navigation_tap is None


def test_a_tap_on_a_second_page_row_becomes_a_card(shop: Shop):
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(SHIRTS))
    _products, more, _button = _rows(first.sent[0])
    _report, second, _model = _tap_more(shop, conversation, more)
    page_two, _more, _button = _rows(second.sent[0])
    model = answer("Here it is.", card={"button_label": "View"})
    _report, transport = shop.turn(
        conversation, model, question="that one",
        metadata={"list_reply_id": rc.row_id(page_two[3]), "list_reply_title": "that one"})
    assert rcard.payload_card(transport.sent[0])["product_id"] == page_two[3]


def test_a_focused_product_stays_a_card_and_opens_no_browse(shop: Shop):
    conversation = shop.conversation()
    before = shop.tokens_for(conversation)

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS)])
        first = _last_search_result(messages)["result"]["products"][0]["product_id"]
        if call == 2:
            return _step([_tool_use("d1", "get_product_details", product_id=first)])
        return _step([_reply("This one.", commerce=True, refs=[f"catalog:product:{first}"],
                             card={"product_id": first, "button_label": "View"})])

    _report, transport = shop.turn(conversation, LiteralModel(script))
    assert rcard.payload_card(transport.sent[0]) is not None
    assert transport.sent[0].get(rc.CHOICES_KEY) is None
    assert shop.tokens_for(conversation) == before


def test_a_comparison_is_exactly_what_it_named_even_with_the_more_word(shop: Shop):
    """A pick of the model's — two of the five it was shown — is never widened,
    and giving the "More" word does not make it a browse."""
    conversation = shop.conversation()
    before = shop.tokens_for(conversation)
    model = browse(SHIRTS, pick=2)
    report, transport = shop.turn(conversation, model)
    products, more, _button = _rows(transport.sent[0])
    assert products == shop.products[SHIRTS][:2] and more is None
    assert report.browse_outcome == br.MODEL_PICK and shop.tokens_for(conversation) == before
    assert len(model.calls) == 2 and report.paging_words is None, "nothing was asked of the model"


def test_a_search_the_model_narrowed_is_its_pick(shop: Shop):
    """The model asked the search for two products. Naming both is still a pick."""
    conversation = shop.conversation()
    before = shop.tokens_for(conversation)

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS, limit=2)])
        ids = [int(p["product_id"]) for p in _last_search_result(messages)["result"]["products"]]
        return _step([_reply("Two good ones.", commerce=True, refs=[f"catalog:product:{i}" for i in ids],
                             choices={"product_ids": ids, "button": BUTTON, "more_label": MORE})])

    model = LiteralModel(script)
    report, transport = shop.turn(conversation, model)
    # The model is still told the store matched more than the two it asked for
    # — "these are all of them" must never rest on a narrowed window — but the
    # narrowed search has no continuation.
    assert model.search_result()["result"]["more_results"] is True
    products, more, _button = _rows(transport.sent[0])
    assert products == shop.products[SHIRTS][:2] and more is None
    assert report.browse_outcome == br.NO_CONTINUATION and shop.tokens_for(conversation) == before


def _asked(model: LiteralModel) -> List[Dict[str, Any]]:
    """What the model was told about its own replies that were not accepted."""
    told = []
    for message in model.calls[-1]["messages"]:
        for block in message.get("content") or []:
            if isinstance(block, Mapping) and block.get("type") == "tool_result" and block.get("is_error"):
                told.append(json.loads(block["content"]))
    return told


# The pilot's own step budget: the words are asked for only with two steps left.
PILOT_STEPS = 4


def words_later(query: str, *, second: Optional[Dict[str, Any]] = None,
                fail: str = "") -> LiteralModel:
    """A browse whose first reply leaves out both words; asked, it answers with
    ``second`` (the words it gives then), or fails in the way ``fail`` names."""
    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("call_search_1", "search_products", query=query)])
        if call == 3 and fail == "provider":
            return {"provider": "anthropic", "status": "sdk_error", "blocks": [], "usage": {}}
        ids = [int(p["product_id"]) for p in _last_search_result(messages)["result"]["products"]]
        if call == 3 and fail == "lookup":
            return _step([_tool_use("d1", "get_product_details", product_id=ids[0])])
        if call == 3 and fail == "truncated":
            return _step([{"type": "text", "text": "partial"}], stop_reason="max_tokens")
        if call == 3 and fail == "unverified":
            return _step([_reply("These are some of the options.", commerce=True,
                                 refs=["catalog:product:999999999"],
                                 choices={"product_ids": ids, "button": BUTTON, "more_label": MORE},
                                 call_id="reply_3")])
        choices: Dict[str, Any] = {"product_ids": ids}
        if call == 3:
            choices.update(second or {})
        return _step([_reply("These are some of the options.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in ids], choices=choices,
                             call_id=f"reply_{call}")])
    return LiteralModel(script)


def test_a_list_that_pages_does_not_wait_on_the_models_optional_words(shop: Shop):
    """The regression for the observation: no ``more_label`` used to mean no paging.

    The platform decides the list is the search's and pages; the words it
    lacks are asked of the model once, and the model's answer is on the list.
    """
    conversation = shop.conversation()
    model = words_later(SHIRTS, second={"button": BUTTON, "more_label": MORE})
    report, transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    ids = shop.products[SHIRTS]
    (told,) = _asked(model)
    assert told["accepted"] is False
    assert [p["code"] for p in told["problems"]] == [br.WORDS_NEEDED]
    assert br.BUTTON_FIELD in told["problems"][0]["detail"] and br.MORE_FIELD in told["problems"][0]["detail"]
    products, more, button = _rows(transport.sent[0])
    assert products == ids[:9] and more["title"] == MORE and button == BUTTON
    assert report.browse_outcome == br.OPENED and report.paging_words == "answered"
    assert len(model.calls) == 3 and report.steps_used == 3
    stored = shop.token_row(nav.token_from_row_id(more["id"]))
    assert stored["more_label"] == MORE and stored["button_label"] == BUTTON


@pytest.mark.parametrize("max_steps", [PILOT_STEPS, 6])
def test_a_list_whose_words_never_come_is_exactly_what_the_model_named(shop: Shop, max_steps):
    """Asked once and still without a word: the model's own selector, never a
    "More" row in words the platform made up, and nothing stored — however many
    steps the budget would still allow."""
    conversation = shop.conversation()
    before = shop.tokens_for(conversation)
    model = words_later(SHIRTS, second={"button": BUTTON})
    report, transport = shop.turn(conversation, model, max_steps=max_steps)
    products, more, button = _rows(transport.sent[0])
    assert products == shop.products[SHIRTS][:5] and more is None and button == BUTTON
    assert report.browse_outcome == br.WORDS_MISSING and report.paging_words == "answered"
    assert len(model.calls) == 3, "asked exactly once"
    assert shop.tokens_for(conversation) == before


@pytest.mark.parametrize("fail,outcome", [
    ("provider", ac.StopReason.PROVIDER_FAILURE.value),
    ("lookup", "ProviderToolRequests"),
    ("truncated", "ProviderInvalid"),
    ("unverified", "verification_failed"),
])
def test_a_words_step_that_fails_still_sends_the_verified_reply(shop: Shop, fail, outcome):
    """Asking must never leave the customer worse off than not asking: however
    the step asked for ends, the reply verified before it goes, verbatim, as
    the model asked it, and a lookup the step requested is never run."""
    conversation = shop.conversation()
    before = shop.tokens_for(conversation)
    model = words_later(SHIRTS, fail=fail)
    report, transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    products, more, _button = _rows(transport.sent[0])
    assert products == shop.products[SHIRTS][:5] and more is None
    assert transport.sent[0]["text"] == "These are some of the options."
    assert report.paging_words == outcome
    assert report.tools_called == ("search_products",)
    assert report.browse_outcome == br.WORDS_MISSING and shop.tokens_for(conversation) == before


def test_no_words_are_asked_for_with_one_step_left(shop: Shop):
    """The step asked for would be the last: a resumed invocation could not
    answer at all. The reply goes as the model asked it, unasked."""
    conversation = shop.conversation()
    model = words_later(SHIRTS, second={"button": BUTTON, "more_label": MORE})
    report, transport = shop.turn(conversation, model, max_steps=3)
    products, more, _button = _rows(transport.sent[0])
    assert products == shop.products[SHIRTS][:5] and more is None
    assert len(model.calls) == 2 and report.paging_words is None
    assert report.browse_outcome == br.WORDS_MISSING


def test_a_result_that_fits_one_list_asks_for_no_more_word(shop: Shop):
    conversation = shop.conversation()
    model = browse(SHOES, more=None)
    report, transport = shop.turn(conversation, model)
    products, more, _button = _rows(transport.sent[0])
    assert products == shop.products[SHOES] and more is None
    assert report.browse_outcome == br.WHOLE and len(model.calls) == 2 and report.paging_words is None


def test_a_selector_the_model_asks_for_on_a_more_turn_stands_down_for_the_page(shop: Shop):
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(BAGS))
    _products, more, _button = _rows(first.sent[0])
    watches = shop.products[WATCHES]

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=WATCHES)])
        return _step([_reply("Also these.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in watches[:2]],
                             choices={"product_ids": watches[:2], "button": BUTTON})])

    report, transport, _model = _tap_more(shop, conversation, more, model=LiteralModel(script))
    products, _more, _button = _rows(transport.sent[0])
    assert products == shop.products[BAGS][9:11], "the tapped page is the answer's shape"
    assert transport.sent[0][rc.WITHHELD_KEY] == rc.NAVIGATION_ANSWERED_FIRST
    assert TITLES[WATCHES] in transport.sent[0]["text"], "its options follow the text as lines"


def test_a_stood_down_selector_repeats_nothing_the_browse_already_listed(shop: Shop):
    """Tenant 1-shaped, 25 September: on a "More" turn the model offered the
    first page's products again. The page is the answer; those products are
    already rows on the customer's screen, so they do not come back as lines.
    A product outside this browse still does — nothing the model meant to offer
    is lost."""
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(BAGS))
    first_page, more, _button = _rows(first.sent[0])
    watch = shop.products[WATCHES][0]

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=BAGS),
                          _tool_use("s2", "search_products", query=WATCHES)])
        offered = [first_page[0], first_page[1], watch]
        return _step([_reply("Here you go.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in offered],
                             choices={"product_ids": offered, "button": BUTTON})])

    report, transport, _model = _tap_more(shop, conversation, more, model=LiteralModel(script))
    products, _more, _button = _rows(transport.sent[0])
    assert products == shop.products[BAGS][9:11], "the tapped page is still the answer's shape"
    body = transport.sent[0]["text"]
    assert transport.sent[0][rc.WITHHELD_KEY] == rc.NAVIGATION_ANSWERED_FIRST
    assert TITLES[WATCHES] in body, "a product outside this browse still follows as a line"
    assert f"{TITLES[BAGS]} 1" not in body and f"{TITLES[BAGS]} 2" not in body, \
        "products the browse already listed are not repeated under the text"


def test_a_product_never_shown_as_a_row_still_follows_as_a_line_on_a_more_turn(shop: Shop):
    """Only rows the customer actually has are left out of the stood-down lines.

    A shirt is sold out when page one is built, so it is never a row; it is back
    in stock by the "More" tap and the model offers it with a shirt that was a
    row. The row is not repeated; the never-shown shirt follows as a line."""
    conversation = shop.conversation()
    shirts = shop.products[SHIRTS]
    hidden = shirts[1]
    with shop.engine.begin() as conn:
        conn.execute(text("UPDATE products SET in_stock = false, stock_quantity = 0 WHERE id = :p"),
                     {"p": hidden})
    try:
        _report, first = shop.turn(conversation, browse(SHIRTS))
        first_page, more, _button = _rows(first.sent[0])
        assert hidden not in first_page
    finally:
        with shop.engine.begin() as conn:
            conn.execute(text("UPDATE products SET in_stock = true, stock_quantity = 5 WHERE id = :p"),
                         {"p": hidden})

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS)])
        offered = [first_page[0], hidden]
        return _step([_reply("Here you go.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in offered],
                             choices={"product_ids": offered, "button": BUTTON})])

    _report, transport, _model = _tap_more(shop, conversation, more, model=LiteralModel(script))
    body = transport.sent[0]["text"]
    lines = [line for line in body.splitlines() if TITLES[SHIRTS] in line]
    assert len(lines) == 1, body
    assert f"{TITLES[SHIRTS]} 2" in lines[0], "the surviving line is the never-shown shirt"
    assert f"{TITLES[SHIRTS]} 1 " not in body and not body.rstrip().endswith(f"{TITLES[SHIRTS]} 1"), \
        "the shirt that was already a row is not repeated"
    assert transport.sent[0][rc.WITHHELD_KEY] == rc.NAVIGATION_ANSWERED_FIRST


def test_a_page_that_cannot_be_spent_with_its_reply_goes_as_lines_and_mints_nothing(shop: Shop):
    """The token expires while the model is answering.

    The reservation's spend is refused, nothing it would have written is
    written, and the answer is reserved again without the list: the page's
    products follow the text as lines.
    """
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(SHIRTS))
    _products, more, _button = _rows(first.sent[0])
    token = nav.token_from_row_id(more["id"])

    def expire() -> None:
        with shop.engine.begin() as conn:
            conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET expires_at = now() - interval "
                              "'1 second' WHERE token = :t"), {"t": token})

    rows_before = shop.tokens_for(conversation)
    model = LiteralModel(lambda _c, _m: _step([_reply("More options:")]), each_call=expire)
    report, transport, _model = _tap_more(shop, conversation, more, model=model)
    assert transport.sent[0].get(rc.CHOICES_KEY) is None
    assert transport.sent[0][br.WITHHELD_KEY] == br.PAGE_AS_LINES
    assert TITLES[SHIRTS] in transport.sent[0]["text"]
    assert shop.token_row(token)["consumed_at"] is None
    assert shop.tokens_for(conversation) == rows_before, "no successor was minted"
    assert report.browse_outcome == br.PAGE_AS_LINES


# ── Off where the relation is absent ─────────────────────────────────────────


def test_without_the_relation_the_turn_is_exactly_what_it_was(pg_admin_dsn: str):
    """At 0111 alone: no candidate read, no more_results, no more_label, no token."""
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, "0111")
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        dormant = _seed(engine)
        model = browse(SHIRTS)
        report, transport = dormant.turn(dormant.conversation(), model)
        assert "more_results" not in model.search_result()["result"]
        assert "more_label" not in model.declared(ap.REPLY_TOOL_NAME)["input_schema"][
            "properties"]["choices"]["properties"]
        assert "more_results" not in model.declared("search_products")["description"]
        products, more, _button = _rows(transport.sent[0])
        # Paging is off: no browse runtime exists, so nothing is even declined.
        assert more is None and len(products) == 5 and report.browse_outcome is None
    finally:
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


# ── What the platform lists is what can be bought now ────────────────────────


def test_hidden_and_sold_out_products_are_never_rows_the_platform_adds(shop: Shop):
    """The stored result keeps its membership; what is listed is decided when shown.

    One product hidden by the merchant and one sold out, among the rows the
    platform adds to page one and on page two: neither is listed, both are
    counted, and nothing takes their place.
    """
    conversation = shop.conversation()
    ids = shop.products[SHIRTS]
    hidden, sold_out, later_hidden = ids[6], ids[7], ids[11]
    with shop.engine.begin() as conn:
        conn.execute(text("UPDATE products SET merchant_hidden_at = now() WHERE id = :p"),
                     {"p": hidden})
        # Sold out by quantity: still flagged in stock, so the search order —
        # stocked first — does not move, and only orderability changes.
        conn.execute(text("UPDATE products SET stock_quantity = 0 WHERE id = :p"), {"p": sold_out})
    try:
        _report, first = shop.turn(conversation, browse(SHIRTS))
        products, more, _button = _rows(first.sent[0])
        assert products == [pid for pid in ids[:9] if pid not in (hidden, sold_out)]
        assert first.sent[0][rc.CHOICES_KEY][rc.NAVIGATION_KEY]["unavailable"] == 2
        assert first.sent[0][rc.CHOICES_KEY][rc.ROW_EVIDENCE_KEY] == [
            rc.product_ref(pid) for pid in products]
        with shop.engine.begin() as conn:
            conn.execute(text("UPDATE products SET merchant_hidden_at = now() WHERE id = :p"),
                         {"p": later_hidden})
        _report, second, model = _tap_more(shop, conversation, more)
        page_two, _more, _button = _rows(second.sent[0])
        assert later_hidden not in page_two and page_two == [pid for pid in ids[9:18]
                                                              if pid != later_hidden]
        assert _facts(model)[br.FACTS_KEY]["no_longer_available"] == 1
    finally:
        with shop.engine.begin() as conn:
            conn.execute(text("UPDATE products SET merchant_hidden_at = NULL, stock_quantity = 5 "
                              "WHERE id = ANY(:p)"), {"p": [hidden, sold_out, later_hidden]})


def test_a_tap_on_a_platform_row_hidden_since_it_was_sent_is_no_selection(shop: Shop):
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(SHIRTS))
    products, _more, _button = _rows(first.sent[0])
    seventh = products[6]
    with shop.engine.begin() as conn:
        conn.execute(text("UPDATE products SET merchant_hidden_at = now() WHERE id = :p"),
                     {"p": seventh})
    try:
        model = answer("Let me check.", card={"button_label": "View"})
        _report, transport = shop.turn(
            conversation, model, question="row seven",
            metadata={"list_reply_id": rc.row_id(seventh), "list_reply_title": "row seven"})
        assert "customer_tapped" not in model.context_block()
        assert rcard.payload_card(transport.sent[0]) is None
    finally:
        with shop.engine.begin() as conn:
            conn.execute(text("UPDATE products SET merchant_hidden_at = NULL WHERE id = :p"),
                         {"p": seventh})


# ── A navigation write the database refuses ──────────────────────────────────


def test_a_page_one_token_the_database_refuses_leaves_exactly_the_models_selector(shop: Shop):
    """The mint fails inside the reservation; nothing it would have written is.

    A trigger refuses every insert into the navigation relation. The
    reservation carrying page one is refused whole, and the answer is reserved
    again as it would be without paging: the products the model named, no
    "More" row, and no token anywhere.
    """
    conversation = shop.conversation()
    with shop.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION refuse_navigation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'navigation refused for this case'; END $$ LANGUAGE plpgsql"""))
        conn.execute(text(f"CREATE TRIGGER refuse_navigation BEFORE INSERT ON {nm.NAVIGATION_TABLE} "
                          "FOR EACH ROW EXECUTE FUNCTION refuse_navigation()"))
    try:
        before = shop.tokens_for(conversation)
        report, transport = shop.turn(conversation, browse(SHIRTS))
        products, more, _button = _rows(transport.sent[0])
        assert more is None and products == shop.products[SHIRTS][:5]
        assert report.choices_outcome == rc.OFFERED and report.browse_outcome is None
        assert shop.tokens_for(conversation) == before
    finally:
        with shop.engine.begin() as conn:
            conn.execute(text(f"DROP TRIGGER refuse_navigation ON {nm.NAVIGATION_TABLE}"))
            conn.execute(text("DROP FUNCTION refuse_navigation()"))


def test_a_page_sent_as_lines_does_not_print_the_models_copy_of_it_again(shop: Shop):
    """The token expires while the model answers, so the page goes as lines. The
    model also offered one of that page's products: it is printed once, with the
    page; a product outside the browse still follows."""
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(BAGS))
    _products, more, _button = _rows(first.sent[0])
    token = nav.token_from_row_id(more["id"])
    page_bag = shop.products[BAGS][9]
    watch = shop.products[WATCHES][0]

    def expire() -> None:
        with shop.engine.begin() as conn:
            conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET expires_at = now() - interval "
                              "'1 second' WHERE token = :t"), {"t": token})

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=f"{TITLES[BAGS]} 10"),
                          _tool_use("s2", "search_products", query=WATCHES)])
        offered = [page_bag, watch]
        return _step([_reply("Also these.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in offered],
                             choices={"product_ids": offered, "button": BUTTON})])

    _report, transport, _model = _tap_more(shop, conversation, more,
                                           model=LiteralModel(script, each_call=expire))
    body = transport.sent[0]["text"]
    assert transport.sent[0][br.WITHHELD_KEY] == br.PAGE_AS_LINES
    assert body.count(f"{TITLES[BAGS]} 10") == 1, body
    assert TITLES[WATCHES] in body


def test_a_page_that_cannot_be_spent_still_outranks_the_models_own_selector(shop: Shop):
    """The degraded path keeps the precedence of the normal one.

    The token expires while the model answers, and the model asked for a
    selector of its own. The page is still the answer — as lines — and the
    model's selector stands down to lines as well: no list goes out at all.
    """
    conversation = shop.conversation()
    _report, first = shop.turn(conversation, browse(BAGS))
    _products, more, _button = _rows(first.sent[0])
    token = nav.token_from_row_id(more["id"])
    watches = shop.products[WATCHES]

    def expire() -> None:
        with shop.engine.begin() as conn:
            conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET expires_at = now() - interval "
                              "'1 second' WHERE token = :t"), {"t": token})

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=WATCHES)])
        return _step([_reply("Also these.", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in watches[:2]],
                             choices={"product_ids": watches[:2], "button": BUTTON})])

    _report, transport, _model = _tap_more(shop, conversation, more,
                                           model=LiteralModel(script, each_call=expire))
    assert transport.sent[0].get(rc.CHOICES_KEY) is None
    assert transport.sent[0][br.WITHHELD_KEY] == br.PAGE_AS_LINES
    assert transport.sent[0][rc.WITHHELD_KEY] == rc.NAVIGATION_ANSWERED_FIRST
    assert TITLES[BAGS] in transport.sent[0]["text"] and TITLES[WATCHES] in transport.sent[0]["text"]
    assert shop.token_row(token)["consumed_at"] is None


def test_a_candidate_read_that_fails_leaves_the_turns_session_usable(shop: Shop, monkeypatch):
    """The platform's read shares the session the model's tools read on.

    It fails here with a real statement error. Without its savepoint the
    session's transaction would be left aborted, and the model's next read in
    the same turn would fail with it. The search still answers, the model is
    told nothing about more results, and the next read succeeds.
    """
    from modules.ai.commerce_agent_v2.tools import catalog

    def failing(context, query, limit):
        context.db.execute(text("SELECT 1/0"))

    monkeypatch.setattr(catalog, "search_product_candidates_impl", failing)
    conversation = shop.conversation()

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS)])
        first = _last_search_result(messages)["result"]["products"][0]["product_id"]
        if call == 2:
            return _step([_tool_use("d1", "get_product_details", product_id=first)])
        return _step([_reply("This one.", commerce=True, refs=[f"catalog:product:{first}"])])

    model = LiteralModel(script)
    report, _transport = shop.turn(conversation, model)
    assert "more_results" not in model.search_result()["result"]
    details = [json.loads(block["content"]) for block in model.calls[-1]["messages"][-1]["content"]
               if isinstance(block, Mapping) and block.get("type") == "tool_result"]
    assert details and details[-1]["tool"] == "get_product_details" and details[-1]["ok"] is True
    assert report.tools_called == ("search_products", "get_product_details")


# ── A list the platform decided, and "other products" asked for in words ─────
#
# Tenant 1, 2026-09-25, turn 66: «وش منتجاتكم الثانية ؟». The search returned
# five of twelve buyable products, the model offered no selector, the policy's
# own decision (several candidates, no focus) was recorded and not composed,
# and the reply went as text claiming those were all of the store's products.


def text_then(query: str, *, second: Optional[Callable[[List[int]], Dict[str, Any]]] = None,
              **search: Any) -> LiteralModel:
    """A browse answered first with text alone, the way turn 66 was. Asked for
    the list, it answers with ``second(ids)`` — its whole next reply — or, with
    no ``second``, resubmits the same text unchanged."""
    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("call_search_1", "search_products", query=query, **search)])
        ids = [int(p["product_id"]) for p in _last_search_result(messages)["result"]["products"]]
        refs = [f"catalog:product:{pid}" for pid in ids]
        if call == 3 and second is not None:
            return _step([_reply("Some of what we have — pick one to see it.", commerce=True, refs=refs,
                                 choices=second(ids), call_id="reply_3")])
        return _step([_reply("Here are our products: " + ", ".join(str(i) for i in ids) + ".",
                             commerce=True, refs=refs, call_id=f"reply_{call}")])
    return LiteralModel(script)


def _with_words(ids: List[int]) -> Dict[str, Any]:
    return {"product_ids": ids, "button": BUTTON, "more_label": MORE}


def test_a_list_the_policy_decides_is_asked_for_and_sent_with_the_models_new_text(shop: Shop):
    conversation = shop.conversation()
    model = text_then(SHIRTS, second=_with_words)
    report, transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    ids = shop.products[SHIRTS]
    (told,) = _asked(model)
    assert [p["code"] for p in told["problems"]] == [rc.LIST_OFFER_NEEDED]
    detail = told["problems"][0]["detail"]
    assert all(str(pid) in detail for pid in ids[:5]) and "more_label" in detail
    products, more, button = _rows(transport.sent[0])
    assert products == ids[:9] and more is not None and button == BUTTON
    # The text is the model's own second reply, carried untouched: nothing was
    # cut from the first one and nothing was written for it.
    assert transport.sent[0]["text"] == "Some of what we have — pick one to see it."
    assert report.list_offer == "answered" and report.browse_outcome == br.OPENED
    assert len(model.calls) == 3


def test_a_model_that_declines_the_list_sends_its_reply_as_it_was(shop: Shop):
    """The decision whether the answer is an offer to choose stays the model's."""
    conversation = shop.conversation()
    model = text_then(SHIRTS)
    report, transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    ids = shop.products[SHIRTS]
    assert [p["code"] for (told,) in [_asked(model)] for p in told["problems"]] == [rc.LIST_OFFER_NEEDED]
    assert rc.payload_rows(transport.sent[0]) == ([], "")
    assert transport.sent[0]["text"] == "Here are our products: " + ", ".join(str(i) for i in ids[:5]) + "."
    assert report.list_offer == "answered" and report.choices_outcome == rc.NOT_REQUESTED


def test_a_reply_that_already_offers_the_list_is_not_asked_again(shop: Shop):
    conversation = shop.conversation()
    report, _transport = shop.turn(conversation, browse(SHIRTS), max_steps=PILOT_STEPS)
    assert report.list_offer is None and report.browse_outcome == br.OPENED


def test_a_list_due_with_no_step_left_is_recorded_as_skipped(shop: Shop):
    conversation = shop.conversation()
    model = text_then(SHIRTS, second=_with_words)
    report, transport = shop.turn(conversation, model, max_steps=2)
    assert report.list_offer == "skipped_no_steps" and len(model.calls) == 2
    assert rc.payload_rows(transport.sent[0]) == ([], "")


def test_an_answer_about_one_product_is_not_asked_for_a_list(shop: Shop):
    """Two products searched, the reply cites one: an answer, not an offer."""
    conversation = shop.conversation()

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS)])
        first = _last_search_result(messages)["result"]["products"][0]["product_id"]
        return _step([_reply("That one is in stock.", commerce=True, refs=[f"catalog:product:{first}"])])

    model = LiteralModel(script)
    report, transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    assert len(model.calls) == 2 and _asked(model) == []
    assert report.list_offer == "skipped_reply_cites_fewer"
    offered = len(model.search_result()["result"]["products"])
    assert report.list_offer_cited == f"1/{offered}"


def test_a_reply_that_names_products_without_citing_them_is_recorded_not_asked(shop: Shop):
    """The platform reads only what the reply cites. A text that names the
    products without citing them goes as written, and the turn report says the
    decided list met a reply citing too few — so such a turn is visible."""
    conversation = shop.conversation()

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS)])
        return _step([_reply("We have several shirts in blue and white.")])

    model = LiteralModel(script)
    report, transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    assert _asked(model) == [] and len(model.calls) == 2
    assert transport.sent[0]["text"] == "We have several shirts in blue and white."
    assert report.list_offer == "skipped_reply_cites_fewer"
    offered = len(model.search_result()["result"]["products"])
    assert offered >= 2 and report.list_offer_cited == f"0/{offered}"


def test_a_comparison_of_two_read_products_is_not_widened_into_the_search(shop: Shop):
    conversation = shop.conversation()

    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("s1", "search_products", query=SHIRTS)])
        ids = [int(p["product_id"]) for p in _last_search_result(messages)["result"]["products"]]
        if call == 2:
            return _step([_tool_use("d1", "get_product_details", product_id=ids[0]),
                          _tool_use("d2", "get_product_details", product_id=ids[1])])
        return _step([_reply("The first is lighter than the second.", commerce=True,
                             refs=[f"catalog:product:{ids[0]}", f"catalog:product:{ids[1]}"])])

    model = LiteralModel(script)
    report, _transport = shop.turn(conversation, model, max_steps=6)
    assert report.list_offer is None and _asked(model) == []


def test_a_narrowed_search_is_not_asked_for_a_more_word(shop: Shop):
    """A search the model narrowed has no continuation, so no "More" row can
    exist and its word is not asked for."""
    conversation = shop.conversation()
    model = text_then(SHIRTS, limit=3)
    report, _transport = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    (told,) = _asked(model)
    assert [p["code"] for p in told["problems"]] == [rc.LIST_OFFER_NEEDED]
    assert "more_label" not in told["problems"][0]["detail"]


def _other_products(query: str, *, reply_choices: bool = True) -> LiteralModel:
    """A customer asking in words for other products: the model searches with
    ``exclude_shown`` and offers what came back."""
    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("call_search_2", "search_products", query=query, exclude_shown=True)])
        ids = [int(p["product_id"]) for p in _last_search_result(messages)["result"]["products"]]
        refs = [f"catalog:product:{pid}" for pid in ids]
        if not ids or not reply_choices:
            return _step([_reply("That is everything else we have.", call_id=f"reply_{call}")])
        return _step([_reply("Here are others.", commerce=True, refs=refs, choices=_with_words(ids),
                             call_id=f"reply_{call}")])
    return LiteralModel(script)


def test_other_products_asked_in_words_then_more_then_a_tap_never_repeat_a_product(shop: Shop):
    """The whole path: a list, other products typed, "More" on the new list,
    and a tap on its last page — every product once, in the search's order."""
    conversation = shop.conversation()
    ids = shop.products[SHIRTS]
    _report, first = shop.turn(conversation, browse(SHIRTS), max_steps=PILOT_STEPS)
    page_one, _more, _button = _rows(first.sent[0])
    assert page_one == ids[:9]

    model = _other_products(SHIRTS)
    report, second = shop.turn(conversation, model, question="وش عندكم غيرها؟", max_steps=PILOT_STEPS)
    shown = model.search_result()["result"]
    # The model sees the next products in the same order, is told how many it
    # has already shown, and that more still follow.
    assert [p["product_id"] for p in shown["products"]] == ids[9:14]
    assert shown["shown_earlier_left_out"] == 9 and shown["more_results"] is True
    others, more, _button = _rows(second.sent[0])
    assert others == ids[9:18] and more is not None
    assert report.browse_outcome == br.OPENED and not set(others) & set(page_one)

    _report, third, _model = _tap_more(shop, conversation, more)
    last, after, _button = _rows(third.sent[0])
    assert last == ids[18:23] and after is None
    assert page_one + others + last == ids, "every product exactly once, in the search's order"

    model = answer("Here it is.", card={"button_label": "View"})
    _report, transport = shop.turn(
        conversation, model, question="that one",
        metadata={"list_reply_id": rc.row_id(last[2]), "list_reply_title": "that one"})
    assert rcard.payload_card(transport.sent[0])["product_id"] == last[2]


def test_other_products_when_everything_was_shown_says_so_and_is_not_a_new_list(shop: Shop):
    conversation = shop.conversation()
    ids = shop.products[WATCHES]
    _report, first = shop.turn(conversation, browse(WATCHES), max_steps=PILOT_STEPS)
    assert _rows(first.sent[0])[0] == ids
    model = _other_products(WATCHES)
    _report, second = shop.turn(conversation, model, question="غيرها؟", max_steps=PILOT_STEPS)
    shown = model.search_result()["result"]
    assert shown["products"] == [] and shown["found"] is False
    assert shown["more_results"] is False and shown["shown_earlier_left_out"] == len(ids)
    assert rc.payload_rows(second.sent[0]) == ([], "")


def test_the_observed_turn_a_general_browse_then_other_products_in_words(shop: Shop):
    """Turn 66's shape on a generic store: an empty-query browse answered as
    text, the list asked for and sent, then «منتجاتكم الثانية» typed."""
    conversation = shop.conversation()
    model = text_then("", second=_with_words)
    report, first = shop.turn(conversation, model, max_steps=PILOT_STEPS)
    browsed = model.search_result()["result"]
    assert len(browsed["products"]) == 5 and browsed["more_results"] is True
    page_one, more, _button = _rows(first.sent[0])
    assert report.list_offer == "answered" and len(page_one) == 9 and more is not None

    model = _other_products("")
    _report, second = shop.turn(conversation, model, question="وش منتجاتكم الثانية ؟",
                                max_steps=PILOT_STEPS)
    shown = model.search_result()["result"]
    assert shown["more_results"] is True and shown["shown_earlier_left_out"] >= 5
    assert not {p["product_id"] for p in shown["products"]} & set(page_one)
    others, _more, _button = _rows(second.sent[0])
    assert others and not set(others) & set(page_one)



def _other_products_in_text(query: str) -> LiteralModel:
    """Other products asked for in words, answered in text that cites them.
    Asked for the list, it resubmits the same text: a walk that never sends a
    row, so only the replies' citations record what was shown."""
    def script(call: int, messages: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if call == 1:
            return _step([_tool_use("call_search_t", "search_products", query=query, exclude_shown=True)])
        ids = [int(p["product_id"]) for p in _last_search_result(messages)["result"]["products"]]
        return _step([_reply("Also: " + ", ".join(str(i) for i in ids) + ".", commerce=True,
                             refs=[f"catalog:product:{pid}" for pid in ids], call_id=f"reply_{call}")])
    return LiteralModel(script)


def test_a_walk_in_text_past_ten_products_never_comes_back_to_its_start(shop: Shop):
    """The model is handed at most ten earlier products; what a request for
    other products leaves out is every product cited within the lapse."""
    conversation = shop.conversation()
    ids = shop.products[SHIRTS]
    _report, _first = shop.turn(conversation, text_then(SHIRTS), max_steps=PILOT_STEPS)
    seen = list(ids[:5])
    for turn in range(3):
        model = _other_products_in_text(SHIRTS)
        shop.turn(conversation, model, question="غيرها؟", max_steps=PILOT_STEPS)
        got = [p["product_id"] for p in model.search_result()["result"]["products"]]
        assert got == ids[5 * (turn + 1):5 * (turn + 2)], (turn, got)
        seen += got
    assert len(seen) == len(set(seen)) == 20


def test_other_products_past_a_capped_read_does_not_claim_more(shop: Shop, monkeypatch):
    """A candidate read that stopped at its cap says nothing about buyable
    products beyond it: with nothing left in what was read, the answer is
    "nothing else to show", never "more exist"."""
    from core.commerce_runtime import search_candidates as sc

    monkeypatch.setattr(sc, "CANDIDATE_CAP", 6)
    conversation = shop.conversation()
    ids = shop.products[SHOES]
    _report, first = shop.turn(conversation, browse(SHOES, more=None), max_steps=PILOT_STEPS)
    assert _rows(first.sent[0])[0] == ids[:6]
    model = _other_products(SHOES)
    shop.turn(conversation, model, question="غيرها؟", max_steps=PILOT_STEPS)
    shown = model.search_result()["result"]
    assert shown["found"] is False and shown["more_results"] is False
