#!/usr/bin/env python3
"""Off-send evaluation of the commerce runtime's reply language and presentation.

What runs is what production runs for a pilot turn: ``run_commerce_runtime_turn``
with the pilot's own instructions, budget, per-turn context
(``commerce_runtime_pilot._context_preamble``), history
(``commerce_runtime_pilot._prior_turns``) and recording
(``commerce_runtime_pilot._record``), the real read tools and the real model.

What does not run: WhatsApp and the production database. The senders record
what would have been sent and answer with a synthetic id; ``DATABASE_URL`` names
a disposable database on this container's own PostgreSQL, created and dropped
here. The only external call is the model's.

Measurement only. The marker lists below classify text for the report. Nothing
here is imported by the runtime, and nothing here changes what the model is
told: the wrapper around the provider records the request, never edits it.

Output: one ``EVAL_TURN=`` JSON line per turn, one ``EVAL_SUMMARY=`` line.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

ADMIN_ENV = "NAHLA_EVAL_ADMIN_DSN"
MODEL_ENV = "EVAL_MODEL"
LABEL = os.environ.get("EVAL_LABEL", "unlabelled")
REPEATS = max(1, min(int(os.environ.get("EVAL_REPEATS", "3") or 3), 10))
SCRIPTED = os.environ.get("EVAL_PROVIDER", "") == "scripted"

# ── Measurement vocabulary (report only) ─────────────────────────────────────
NON_SAUDI_MARKERS = ("هسع", "شنو", "شو ", "هلأ", "هلق", "بدك", "كتير", "إزاي", "ازاي", "عايز",
                     "دلوقتي", "ماكو", "وايد", "شلون", "تريد", "راح ")
SAUDI_MARKERS = ("وش", "تبي", "تبغى", "تبغين", "تبين", "أبشر", "ابشر", "حياك", "يا هلا", "الحين",
                 "عشان", "مره", "مرة حلو", "أبي", "ابي")
PRICE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:ريال|ر\.س|SAR|sar)")
LATIN = re.compile(r"[A-Za-z]")
ARABIC = re.compile(r"[؀-ۿ]")

# ── Catalogue (generic clothing merchant, shaped like a real synced store) ───
# (title, price, stock, sizes, colour, has_image)
CATALOGUE_A: Tuple[Tuple[str, int, int, Tuple[str, ...], str, bool], ...] = (
    ("فستان", 289, 6, ("38 - XS", "36 - S"), "فوشي", True),
    ("فستان", 249, 3, ("40 - M", "38 - S"), "أسود", True),
    ("فستان", 199, 4, ("42 - L", "40 - M"), "أبيض", True),
    ("فستان", 319, 2, ("38 - S",), "كحلي", True),
    ("فستان", 179, 5, ("36 - XS", "40 - M"), "وردي", True),
    ("جاكيت", 169, 2, ("44 - XL", "38 - M", "40 - S"), "بيج", True),
    ("بنطلون", 169, 2, ("38 - S",), "أسود", True),
    ("بنطلون", 74, 5, ("36 - XS", "44 - XL", "38 - S", "42 - L"), "أزرق", True),
    ("تنورة", 114, 1, ("40 - M", "38 - S"), "رمادي", True),
    ("بلوزة", 59, 5, ("40 - M", "38 - S", "36 - XS", "44 - XL", "42 - L"), "أبيض", True),
    ("قميص قطني أزرق", 99, 7, ("M", "L"), "أزرق", True),
    ("حذاء رياضي أبيض", 199, 4, ("40", "41", "42"), "أبيض", True),
    ("شنطة يد جلد", 229, 3, (), "بني", False),
)
CATALOGUE_B: Tuple[Tuple[str, int, int, Tuple[str, ...], str, bool], ...] = (
    ("عطر ورد 100ml", 180, 6, (), "", True),
    ("قميص قطني أزرق", 99, 7, ("M", "L"), "أزرق", True),
    ("حذاء رياضي أبيض", 199, 4, ("40", "41", "42"), "أبيض", True),
    ("شنطة يد جلد", 229, 3, (), "بني", True),
)
STORE_HOST = "shop.eval-store.example"
IMAGE_HOST = "cdn.eval-store.example"

# The conversation Tenant 1 actually had before the observed turns, as the
# customer saw it (September 25, 2026 screenshots). Used as prior history only.
DRIFT_GREETING = "شنو اللي بساعدك فيه اليوم؟ 😊"
DRIFT_IDENTITY = ("أنا وردة 👋، وكيل مبيعات وخدمة عملاء ذكية هنا عندكم! 😊\n\n"
                  "أنا هسع هنا لمساعدتك في:\n✨ تصفح منتجاتنا الرائعة\n"
                  "💰 إيجاد أفضل الأسعار والكوبونات\n🛍️ الإجابة على أسئلتك عن المنتجات\n"
                  "📦 متابعة طلبياتك\n🚚 معرفة حالة الشحنة\n\nشنو اللي بساعدك فيه اليوم؟ 😊")

TAP_PRODUCT = "tap_product"
TAP_MORE = "tap_more"


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    tenant: str                                  # "A" (arabic) or "B" (english)
    steps: Tuple[str, ...]                       # customer text, or a tap marker
    history: Tuple[Tuple[str, str], ...] = ()    # (direction, body) seeded before step one
    tap_title: str = "بلوزة"                     # which product row a product tap picks


SCENARIOS: Tuple[Scenario, ...] = (
    Scenario("identity_fresh", "A", ("من انت؟",)),
    Scenario("identity_after_drift", "A", ("من انت",), history=(("outbound", DRIFT_GREETING),)),
    Scenario("browse_select_link_fresh", "A",
             ("وش المنتجات المتوفرة عندكم؟", TAP_PRODUCT, "ارسل لي رابط المنتج")),
    Scenario("browse_more_fresh", "A", ("وش المنتجات المتوفرة عندكم؟", TAP_MORE)),
    Scenario("browse_select_after_drift", "A", ("وش المنتجات المتوفرة عندكم؟", TAP_PRODUCT),
             history=(("outbound", DRIFT_GREETING), ("inbound", "من انت"),
                      ("outbound", DRIFT_IDENTITY))),
    Scenario("compare", "A", ("وش الفرق بين الجاكيت والتنورة؟",)),
    Scenario("details_requested", "A", ("ابي كل تفاصيل البلوزة: السعر والمقاسات والكمية المتوفرة",)),
    Scenario("product_without_photo", "A", ("عندكم شنطة يد؟ ابي أشوفها",)),
    Scenario("english_setting_english_customer", "B", ("What products do you have?",)),
    Scenario("english_setting_arabic_customer", "B", ("وش عندكم من منتجات؟",)),
)


# ── Output hygiene ───────────────────────────────────────────────────────────


def _secrets() -> List[str]:
    values = []
    for key, value in os.environ.items():
        if value and len(value) >= 12 and any(k in key.upper() for k in ("KEY", "TOKEN", "SECRET",
                                                                         "PASSWORD", "DSN", "URL")):
            values.append(value)
    return values


SECRETS = _secrets()


def emit(prefix: str, payload: Mapping[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    for secret in SECRETS:
        line = line.replace(secret, "[redacted]")
    print(prefix + line, flush=True)


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── Seed ─────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Store:
    tenant_id: int
    connection_id: int
    product_ids: List[int]
    prices: set
    image_urls: List[str]
    product_urls: List[str]


def seed_store(engine: Any, label: str, settings: Mapping[str, Any],
               catalogue: Sequence[Tuple[str, int, int, Tuple[str, ...], str, bool]]) -> Store:
    from sqlalchemy import text

    with engine.begin() as conn:
        tenant_id = int(conn.execute(text(
            "INSERT INTO tenants (name, is_active, is_platform_tenant) VALUES (:n, true, false) "
            "RETURNING id"), {"n": f"متجر تجريبي عام {label}"}).scalar_one())
        conn.execute(text("INSERT INTO tenant_settings (tenant_id, show_nahla_branding, "
                          "branding_text, ai_settings) VALUES (:t, true, '', CAST(:s AS JSONB))"),
                     {"t": tenant_id, "s": json.dumps(dict(settings), ensure_ascii=False)})
        connection_id = int(conn.execute(text(
            "INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
            "VALUES (:t, :p, 'connected') RETURNING id"),
            {"t": tenant_id, "p": "1555" + uuid.uuid4().hex[:8]}).scalar_one())
        product_ids, prices, images, links = [], set(), [], []
        for index, (title, price, stock, sizes, colour, has_image) in enumerate(catalogue, start=1):
            ref = f"{label}{index:02d}{uuid.uuid4().hex[:6]}"
            image = f"https://{IMAGE_HOST}/{ref}.jpg" if has_image else ""
            link = f"https://{STORE_HOST}/p/{ref}"
            meta = {"product_url": link, "currency": "SAR", "status": "active"}
            if image:
                meta["image_url"] = image
                images.append(image)
            links.append(link)
            prices.add(str(price))
            product_id = int(conn.execute(text(
                "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
                "stock_quantity, metadata) VALUES (:t, :x, :ti, :d, :p, true, :q, CAST(:m AS JSONB)) "
                "RETURNING id"),
                {"t": tenant_id, "x": "SKU-" + ref, "ti": title, "d": f"{title} {colour}".strip(),
                 "p": str(price), "q": stock, "m": json.dumps(meta, ensure_ascii=False)}).scalar_one())
            product_ids.append(product_id)
            for size_index, size in enumerate(sizes):
                options = {"المقاس": size}
                if colour:
                    options["اللون"] = colour
                conn.execute(text(
                    "INSERT INTO product_variants (tenant_id, product_id, sku, price, currency, "
                    "stock_quantity, in_stock, options, option_summary, is_default) "
                    "VALUES (:t, :p, :s, :pr, 'SAR', :q, true, CAST(:o AS JSONB), :os, false)"),
                    {"t": tenant_id, "p": product_id, "s": f"SKU-{ref}-{size_index}", "pr": str(price),
                     "q": max(1, stock // max(1, len(sizes))),
                     "o": json.dumps(options, ensure_ascii=False),
                     "os": " / ".join(options.values())})
            if sizes:
                conn.execute(text("UPDATE products SET has_variants = true WHERE id = :p"),
                             {"p": product_id})
    return Store(tenant_id=tenant_id, connection_id=connection_id, product_ids=product_ids,
                 prices=prices, image_urls=images, product_urls=links)


def new_conversation(engine: Any, store: Store, name: str) -> Tuple[int, int, str]:
    from sqlalchemy import text

    phone = "+96650" + str(uuid.uuid4().int)[:7]
    with engine.begin() as conn:
        customer_id = int(conn.execute(text(
            "INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
            "VALUES (:t, :p, :p, :n) RETURNING id"),
            {"t": store.tenant_id, "p": phone, "n": name}).scalar_one())
        conversation_id = int(conn.execute(text(
            "INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
            "VALUES (:t, :c, :e, 'active') RETURNING id"),
            {"t": store.tenant_id, "c": customer_id, "e": phone}).scalar_one())
    return customer_id, conversation_id, phone


# ── Transport and provider doubles (record only) ─────────────────────────────


class Senders:
    """What would have gone to WhatsApp. Every send is accepted with a synthetic id."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def _ok(self) -> Tuple[str, str, int]:
        return "ok", "wamid.eval." + uuid.uuid4().hex, 200

    def text(self, recipient: str, body: str) -> Tuple[str, str, int]:
        self.sent.append({"kind": "text", "text": body})
        return self._ok()

    def list(self, recipient: str, body: str, rows: Any, button: str) -> Tuple[str, str, int]:
        self.sent.append({"kind": "list", "text": body, "button": button,
                          "rows": [dict(row) for row in rows]})
        return self._ok()

    def card(self, recipient: str, body: str, image_url: str, button_url: str,
             button_label: str) -> Tuple[str, str, int]:
        self.sent.append({"kind": "card", "text": body, "image_url": image_url,
                          "button_url": button_url, "button_label": button_label})
        return self._ok()


class RecordingProvider:
    """The real provider, observed: each request's system text and opening context."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: List[Dict[str, Any]] = []

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        self.requests.append({"system": kwargs.get("system"), "messages": kwargs.get("messages")})
        return self.inner.call_single_step(**kwargs)

    def context_seen(self) -> Dict[str, Any]:
        if not self.requests:
            return {}
        for message in reversed(self.requests[0].get("messages") or []):
            if message.get("role") != "user":
                continue
            for block in message.get("content") or []:
                body = block.get("text") if isinstance(block, dict) else None
                if isinstance(body, str) and body.startswith("<conversation_context>"):
                    try:
                        return json.loads(body.split("\n", 1)[1].rsplit("\n", 1)[0])
                    except ValueError:
                        return {"unparsed": True}
        return {}


class ScriptedModel:
    """Local smoke only: search, then a reply offering the first five as a selector."""

    def __init__(self) -> None:
        self.calls = 0

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        from core.commerce_runtime import agent_provider as ap

        self.calls += 1
        messages = kwargs.get("messages") or []
        results = [b for m in messages if m.get("role") == "user"
                   for b in (m.get("content") or []) if isinstance(b, dict)
                   and b.get("type") == "tool_result"]
        def step(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
            return {"provider": "anthropic", "model": "scripted", "status": "ok",
                    "stop_reason": "tool_use", "blocks": blocks,
                    "usage": {"input_tokens": 10, "output_tokens": 5}, "request_id": "r"}
        if not results:
            return step([{"type": "tool_use", "id": f"s{self.calls}", "name": "search_products",
                          "input": {"query": ""}}])
        body = json.loads(results[-1]["content"]) if isinstance(results[-1].get("content"), str) else {}
        found = (body.get("result") or {}).get("products") or []
        ids = [p.get("product_id") for p in found if p.get("product_id")][:5]
        refs = [p.get("evidence_ref") for p in found if p.get("evidence_ref")][:5]
        reply: Dict[str, Any] = {"text": "هذي منتجاتنا", "evidence_refs": refs,
                                 "claims_commerce_facts": True}
        if len(ids) >= 2:
            reply["choices"] = {"product_ids": ids, "button": "اختار", "more_label": "المزيد"}
        reply["card"] = {"button_label": "شوف المنتج"}
        return step([{"type": "tool_use", "id": f"r{self.calls}", "name": ap.REPLY_TOOL_NAME,
                      "input": reply}])


# ── One turn ─────────────────────────────────────────────────────────────────


class _Trace:
    def mark_outbound_sent(self, **_kw: Any) -> None:
        return None


def run_turn(ctx: "Context", store: Store, conversation_id: int, customer_id: int, phone: str,
             customer_name: str, text_in: str, metadata: Mapping[str, Any]) -> Dict[str, Any]:
    from core.commerce_runtime import pilot_guard
    from core.commerce_runtime import runtime_entry as entry
    from core.conversation_engine import StateManager
    from services import commerce_runtime_pilot as seam
    from sqlalchemy import text as sql

    db = ctx.session_factory()
    try:
        StateManager.save_message(db, phone, text_in, "inbound", conversation_id=conversation_id,
                                  tenant_id=store.tenant_id)
        db.commit()
        from models import Conversation
        convo = db.get(Conversation, conversation_id)      # the ORM row, as the webhook passes it
        preamble = seam._context_preamble(db, store.tenant_id, convo, customer_name)
        history = seam._prior_turns(db, tenant_id=store.tenant_id, conversation_id=conversation_id,
                                    phone=phone, current_text=text_in)
        db.commit()
    finally:
        db.close()
    senders = Senders()
    if SCRIPTED:
        provider = RecordingProvider(ScriptedModel())
    else:
        from modules.ai.orchestrator.providers.anthropic_provider import AnthropicProvider
        provider = RecordingProvider(AnthropicProvider())
    started = time.monotonic()
    report = entry.run_commerce_runtime_turn(
        engine=ctx.engine, session_factory=ctx.session_factory, tenant_id=store.tenant_id,
        conversation_id=conversation_id, connection_ref=f"wa:{store.connection_id}",
        connection_id=str(store.connection_id), customer_id=customer_id,
        normalized_customer_phone=phone,
        provider_message_id="wamid.eval.in." + uuid.uuid4().hex, inbound_text=text_in,
        inbound_metadata=dict(metadata), transport=entry.whatsapp_reply_transport(
            senders.text, senders.list, recipient=phone, send_card=senders.card),
        instructions=seam._instructions(), model=ctx.model, budget=pilot_guard.pilot_budget(),
        context_preamble=preamble, history=history, anthropic_provider=provider)
    wall_ms = int((time.monotonic() - started) * 1000)
    last = senders.sent[-1] if senders.sent else {}
    wire = seam.WireObservation()
    if last:
        wire.record(str(last.get("text") or ""), [], duplicate_suppressed=False,
                    row_ids=[str(r.get("id")) for r in last.get("rows") or []])
    db = ctx.session_factory()
    try:
        convo = db.execute(sql("SELECT id FROM conversations WHERE id = :c"),
                           {"c": conversation_id}).first()
        seam._record(db=db, trace=_Trace(), convo=convo, tenant_id=store.tenant_id, to=phone,
                     report=report, wire=wire)
        db.commit()
    finally:
        db.close()
    fields = report.as_log_fields()
    seen = provider.context_seen()
    return {"report": fields, "sent": last, "sends": len(senders.sent), "wall_ms": wall_ms,
            "provider_message_id": report.provider_message_id,
            "context_keys": sorted(seen.keys()),
            "context_reply_language": seen.get("reply_language"),
            "context_reply_tone": seen.get("reply_tone"),
            "context_assistant_name": seen.get("assistant_name"),
            "system_chars": len(str(provider.requests[0].get("system") or "")) if provider.requests else 0}


def measure(result: Mapping[str, Any], store: Store) -> Dict[str, Any]:
    sent = result.get("sent") or {}
    body = str(sent.get("text") or "")
    rows = [r for r in (sent.get("rows") or []) if not str(r.get("id", "")).startswith("nahla:more:")]
    row_prices = set()
    for row in rows:
        match = re.match(r"(\d+(?:\.\d+)?)", str(row.get("description") or ""))
        if match:
            row_prices.add(match.group(1).split(".")[0])
    text_prices = {m.group(1).split(".")[0].replace(",", "") for m in PRICE_PATTERN.finditer(body)}
    sizes_in_text = len(re.findall(r"\d{2}\s*-\s*(?:XS|S|M|L|XL)", body))
    letters = len(LATIN.findall(body)) + len(ARABIC.findall(body))
    return {
        "text_chars": len(body),
        "lines": body.count("\n") + 1 if body else 0,
        "non_saudi_markers": [m.strip() for m in NON_SAUDI_MARKERS if m in body + " "],
        "saudi_markers": [m for m in SAUDI_MARKERS if m in body],
        "latin_ratio": round(len(LATIN.findall(body)) / letters, 2) if letters else 0.0,
        "rows": len(rows),
        "more_row": any(str(r.get("id", "")).startswith("nahla:more:") for r in sent.get("rows") or []),
        "row_prices_in_text": len(row_prices & text_prices),
        "prices_in_text": sorted(text_prices),
        "unknown_prices_in_text": sorted(p for p in text_prices if p not in store.prices),
        "sizes_in_text": sizes_in_text,
        "image_url_in_text": any(u in body for u in store.image_urls) or IMAGE_HOST in body,
        "product_url_in_text": any(u in body for u in store.product_urls) or STORE_HOST in body,
        "card": bool(sent.get("kind") == "card"),
    }


@dataclasses.dataclass
class Context:
    engine: Any
    session_factory: Any
    model: str
    stores: Dict[str, Store]


def run_scenario(ctx: Context, scenario: Scenario, rep: int) -> List[Dict[str, Any]]:
    from core.conversation_engine import StateManager

    store = ctx.stores[scenario.tenant]
    name = "نورة عبدالله" if scenario.tenant == "A" else "أحمد سالم"
    customer_id, conversation_id, phone = new_conversation(ctx.engine, store, name)
    if scenario.history:
        db = ctx.session_factory()
        try:
            for direction, body in scenario.history:
                StateManager.save_message(db, phone, body, direction, conversation_id=conversation_id,
                                          tenant_id=store.tenant_id,
                                          extra_metadata={"eval_seeded_history": True})
            db.commit()
        finally:
            db.close()
    out: List[Dict[str, Any]] = []
    previous: Optional[Dict[str, Any]] = None
    for step_no, step in enumerate(scenario.steps, start=1):
        metadata: Dict[str, Any] = {}
        text_in = step
        if step in (TAP_PRODUCT, TAP_MORE):
            rows = list(((previous or {}).get("sent") or {}).get("rows") or [])
            if step == TAP_MORE:
                chosen = next((r for r in rows if str(r.get("id", "")).startswith("nahla:more:")), None)
            else:
                products = [r for r in rows if str(r.get("id", "")).startswith("nahla:choice:")]
                chosen = next((r for r in products
                               if str(r.get("title", "")).startswith(scenario.tap_title)),
                              products[-1] if products else None)
            if chosen is None:
                out.append({"label": LABEL, "scenario": scenario.name, "rep": rep, "step": step_no,
                            "input": step, "skipped": "no_row_to_tap"})
                break
            text_in = str(chosen.get("title") or "")
            metadata = {"list_reply_id": str(chosen.get("id")), "list_reply_title": text_in,
                        "list_reply_context_id": str((previous or {}).get("provider_message_id") or "")}
        try:
            result = run_turn(ctx, store, conversation_id, customer_id, phone, name, text_in, metadata)
        except Exception as exc:  # noqa: BLE001 - one failed turn is a result, not the end
            out.append({"label": LABEL, "scenario": scenario.name, "rep": rep, "step": step_no,
                        "input": step, "error": type(exc).__name__, "detail": str(exc)[:200]})
            break
        report = result["report"]
        record = {
            "label": LABEL, "scenario": scenario.name, "rep": rep, "step": step_no, "input": step,
            "inbound_text": text_in, "reply_text": (result.get("sent") or {}).get("text"),
            "delivery": (result.get("sent") or {}).get("kind"), "sends": result["sends"],
            "list_button": (result.get("sent") or {}).get("button"),
            "row_titles": [r.get("title") for r in (result.get("sent") or {}).get("rows") or []],
            "card_button": (result.get("sent") or {}).get("button_label"),
            "context_keys": result["context_keys"],
            "context_reply_language": result["context_reply_language"],
            "context_reply_tone": result["context_reply_tone"],
            "context_assistant_name": result["context_assistant_name"],
            "system_chars": result["system_chars"], "wall_ms": result["wall_ms"],
            **{k: report.get(k) for k in (
                "reason", "choices_outcome", "card_outcome", "browse_outcome", "paging_words",
                "navigation_tap", "navigation_page", "navigation_has_next", "steps_used",
                "tool_calls_used", "tools_called", "evidence_refs", "input_tokens",
                "output_tokens", "latency_ms", "model", "stop_reason")},
            "metrics": measure(result, store),
        }
        emit("EVAL_TURN=", record)
        out.append(record)
        previous = result
    return out


def summarise(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by: Dict[str, List[Mapping[str, Any]]] = {}
    for record in records:
        if "metrics" in record:
            by.setdefault(f"{record['scenario']}#{record['step']}", []).append(record)

    def med(values: List[Any]) -> Any:
        values = [v for v in values if isinstance(v, (int, float))]
        return statistics.median(values) if values else None

    summary = {}
    for key, items in sorted(by.items()):
        metrics = [i["metrics"] for i in items]
        summary[key] = {
            "n": len(items),
            "text_chars_median": med([m["text_chars"] for m in metrics]),
            "non_saudi_turns": sum(1 for m in metrics if m["non_saudi_markers"]),
            "row_prices_in_text_median": med([m["row_prices_in_text"] for m in metrics]),
            "sizes_in_text_median": med([m["sizes_in_text"] for m in metrics]),
            "image_url_in_text_turns": sum(1 for m in metrics if m["image_url_in_text"]),
            "product_url_in_text_turns": sum(1 for m in metrics if m["product_url_in_text"]),
            "unknown_price_turns": sum(1 for m in metrics if m["unknown_prices_in_text"]),
            "deliveries": sorted({str(i.get("delivery")) for i in items}),
            "input_tokens_median": med([i.get("input_tokens") for i in items]),
            "output_tokens_median": med([i.get("output_tokens") for i in items]),
            "latency_ms_median": med([i.get("latency_ms") for i in items]),
            "steps_median": med([i.get("steps_used") for i in items]),
        }
    return summary


def main() -> int:
    admin = os.environ.get(ADMIN_ENV, "")
    model = os.environ.get(MODEL_ENV, "") if not SCRIPTED else "scripted-model"
    if not admin or not model or (not SCRIPTED and not (os.environ.get("ANTHROPIC_API_KEY")
                                                         or os.environ.get("CLAUDE_API_KEY"))):
        emit("EVAL_SUMMARY=", {"label": LABEL, "status": "usage",
                               "needs": [ADMIN_ENV, MODEL_ENV, "ANTHROPIC_API_KEY"]})
        return 2
    probe = _load(APP_ROOT / "scripts/operators/commerce_runtime_synthetic_probe.py", "eval_probe")
    name, dsn = probe.create_database(admin)
    os.environ["DATABASE_URL"] = dsn
    records: List[Dict[str, Any]] = []
    try:
        probe.migrate(dsn, "0111")
        probe.migrate(dsn, "0113")
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.commerce_runtime import navigation as nav
        from core.commerce_runtime import runtime_entry as entry

        engine = create_engine(dsn, future=True)
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        stores = {
            "A": seed_store(engine, "A", {"assistant_name": "وردة", "default_language": "arabic",
                                          "reply_tone": "friendly"}, CATALOGUE_A),
            "B": seed_store(engine, "B", {"assistant_name": "Atlas", "default_language": "english",
                                          "reply_tone": "professional"}, CATALOGUE_B),
        }
        ctx = Context(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False),
                      model=model, stores=stores)
        wanted = [s.strip() for s in os.environ.get("EVAL_SCENARIOS", "").split(",") if s.strip()]
        scenarios = [s for s in SCENARIOS if not wanted or s.name in wanted]
        for rep in range(1, REPEATS + 1):
            for scenario in scenarios:
                records.extend(run_scenario(ctx, scenario, rep))
        emit("EVAL_SUMMARY=", {"label": LABEL, "status": "done", "model": model,
                               "repeats": REPEATS, "turns": sum(1 for r in records if "metrics" in r),
                               "errors": [r for r in records if "error" in r or "skipped" in r],
                               "by_step": summarise(records)})
        return 0
    except Exception as exc:  # noqa: BLE001
        emit("EVAL_SUMMARY=", {"label": LABEL, "status": "failed", "error": type(exc).__name__,
                               "detail": str(exc)[:300]})
        return 3
    finally:
        try:
            probe.drop_database(admin, name)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
