#!/usr/bin/env python3
"""Off-send evaluation: store knowledge on delivery, fees, returns and payment.

What runs is what production runs for a pilot turn: ``run_commerce_runtime_turn``
with the pilot's own instructions, budget, per-turn context
(``commerce_runtime_pilot._context_preamble``), history
(``commerce_runtime_pilot._prior_turns``) and recording
(``commerce_runtime_pilot._record``), the real read tools and the real model.
The application code is whatever this commit holds: the same harness runs on
the baseline commit and on the candidate commit, one image each.

What does not run: WhatsApp and the production database. The senders record
what would have been sent and answer with a synthetic id; ``DATABASE_URL`` names
a disposable database on this container's own PostgreSQL, created and dropped
here. The only external call is the model's, under a hard spend cap.

Measurement only: nothing here changes what the model is told; the wrapper
around the provider records the request, never edits it.

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
from typing import Any, Dict, List, Mapping, Sequence, Tuple

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

ADMIN_ENV = "NAHLA_EVAL_ADMIN_DSN"
MODEL_ENV = "EVAL_MODEL"
LABEL = os.environ.get("EVAL_LABEL", "unlabelled")
REPEATS = max(1, min(int(os.environ.get("EVAL_REPEATS", "3") or 3), 10))
SCRIPTED = os.environ.get("EVAL_PROVIDER", "") == "scripted"

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# ── Stores (generic merchants) ───────────────────────────────────────────────
# (title, price, stock, sizes, colour, has_image)
CLOTHING: Tuple[Tuple[str, int, int, Tuple[str, ...], str, bool], ...] = (
    ("فستان", 249, 3, ("40 - M", "38 - S"), "أسود", True),
    ("بلوزة", 59, 5, ("40 - M", "38 - S"), "أبيض", True),
    ("قميص قطني أزرق", 99, 7, ("M", "L"), "أزرق", True),
    ("حذاء رياضي أبيض", 199, 4, ("40", "41", "42"), "أبيض", True),
)
PERFUME: Tuple[Tuple[str, int, int, Tuple[str, ...], str, bool], ...] = (
    ("عطر ورد 100ml", 180, 6, (), "", True),
    ("عطر عود 50ml", 260, 4, (), "", True),
    ("بخور معطر", 90, 10, (), "", True),
    ("كريم مرطب لليدين", 45, 12, (), "", True),
)
# (kind, title, body)
KNOWLEDGE_F: Tuple[Tuple[str, str, str], ...] = (
    ("custom", "التوصيل", "نوصّل لجميع مدن المملكة خلال 2 إلى 5 أيام عمل، والتوصيل مجاني للطلبات فوق 300 ريال."),
    ("custom", "تغليف الهدايا", "نوفّر تغليف هدايا مجانيًا لأي طلب، اطلبه عند إتمام الطلب."),
)
KNOWLEDGE_J: Tuple[Tuple[str, str, str], ...] = (
    ("shipping_policy", "الشحن والتوصيل",
     "الشحن داخل الرياض خلال يوم عمل واحد، وباقي مدن المملكة من 2 إلى 4 أيام عمل. رسوم الشحن 25 ريال، "
     "والشحن مجاني للطلبات فوق 200 ريال. لا نشحن خارج المملكة حاليًا."),
    ("return_policy", "الاستبدال والاسترجاع",
     "يمكن استبدال المنتج أو استرجاعه خلال 7 أيام من الاستلام بشرط أن يكون مغلقًا بحالته الأصلية. "
     "العطور المفتوحة لا تُسترجع."),
    ("payment_method", "طرق الدفع",
     "نقبل مدى وفيزا وApple Pay، والدفع عند الاستلام متاح برسوم 15 ريال."),
)


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    store: str
    message: str


SCENARIOS: Tuple[Scenario, ...] = (
    # Store F: the evaluation store whose delivery section long queries missed.
    Scenario("kb_f_riyadh", "F", "توصلون للرياض؟"),
    Scenario("kb_f_fee", "F", "كم رسوم التوصيل؟"),
    Scenario("kb_f_fee_jeddah_time", "F", "كم رسوم التوصيل لجدة وكم يوم ياخذ؟"),
    Scenario("kb_f_under_threshold", "F", "لو طلبي بـ 150 ريال فيه رسوم توصيل؟"),
    Scenario("kb_f_abroad", "F", "توصلون للكويت؟"),
    Scenario("kb_f_giftwrap", "F", "عندكم تغليف هدايا؟"),
    # Store J: another category, with shipping, returns and payment sections.
    Scenario("kb_j_ship_jeddah", "J", "كم الشحن لجدة؟ ومتى يوصل؟"),
    Scenario("kb_j_return_opened", "J", "فتحت العطر وما عجبني، اقدر ارجعه؟"),
    Scenario("kb_j_cod", "J", "فيه دفع عند الاستلام؟ كم رسومه؟"),
    # Store A: no knowledge at all — the fix must change nothing here.
    Scenario("kb_a_no_section", "A", "توصلون للرياض؟"),
)
NAMES = {"F": "أحمد سالم", "J": "نورة عبدالله", "A": "سارة محمد"}

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
    numbers: set          # every number a customer could be told truthfully: prices, knowledge


def seed_store(engine: Any, label: str, catalogue: Sequence[Tuple[str, int, int, Tuple[str, ...], str, bool]],
               knowledge: Sequence[Tuple[str, str, str]] = ()) -> Store:
    from sqlalchemy import text

    numbers: set = set()
    with engine.begin() as conn:
        tenant_id = int(conn.execute(text(
            "INSERT INTO tenants (name, is_active, is_platform_tenant) VALUES (:n, true, false) "
            "RETURNING id"), {"n": f"متجر تجريبي عام {label}"}).scalar_one())
        conn.execute(text("INSERT INTO tenant_settings (tenant_id, show_nahla_branding, "
                          "branding_text, ai_settings) VALUES (:t, true, '', CAST(:s AS JSONB))"),
                     {"t": tenant_id, "s": json.dumps({"assistant_name": "وردة", "default_language": "arabic",
                                                       "reply_tone": "friendly"}, ensure_ascii=False)})
        connection_id = int(conn.execute(text(
            "INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status, connection_type, "
            "extra_metadata) VALUES (:t, :p, 'connected', 'direct', CAST('{}' AS JSONB)) RETURNING id"),
            {"t": tenant_id, "p": "1555" + uuid.uuid4().hex[:8]}).scalar_one())
        for kind, title, body in knowledge:
            conn.execute(text(
                "INSERT INTO merchant_knowledge_sections (tenant_id, kind, title, body, is_active, "
                "source, ai_status, priority, created_at, updated_at) VALUES (:t, :k, :ti, :b, "
                "true, 'manual', 'approved', 100, now(), now())"),
                {"t": tenant_id, "k": kind, "ti": title, "b": body})
            numbers |= set(NUMBER.findall(body))
        for index, (title, price, stock, sizes, colour, has_image) in enumerate(catalogue, start=1):
            ref = f"{label}{index:02d}{uuid.uuid4().hex[:6]}"
            meta = {"product_url": f"https://shop.eval-store.example/p/{ref}", "currency": "SAR",
                    "status": "active"}
            if has_image:
                meta["image_url"] = f"https://cdn.eval-store.example/{ref}.jpg"
            numbers.add(str(price))
            product_id = int(conn.execute(text(
                "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
                "stock_quantity, metadata) VALUES (:t, :x, :ti, :d, :p, true, :q, CAST(:m AS JSONB)) "
                "RETURNING id"),
                {"t": tenant_id, "x": "SKU-" + ref, "ti": title, "d": f"{title} {colour}".strip(),
                 "p": str(price), "q": stock, "m": json.dumps(meta, ensure_ascii=False)}).scalar_one())
            numbers |= set(NUMBER.findall(title))
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
                     "o": json.dumps(options, ensure_ascii=False), "os": " / ".join(options.values())})
                numbers |= set(NUMBER.findall(size))
            if sizes:
                conn.execute(text("UPDATE products SET has_variants = true WHERE id = :p"), {"p": product_id})
    return Store(tenant_id=tenant_id, connection_id=connection_id, numbers=numbers)


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
        self.sent.append({"kind": "list", "text": body, "button": button, "rows": [dict(r) for r in rows]})
        return self._ok()

    def card(self, recipient: str, body: str, image_url: str, button_url: str,
             button_label: str) -> Tuple[str, str, int]:
        self.sent.append({"kind": "card", "text": body, "button_label": button_label})
        return self._ok()


class RecordingProvider:
    """The real provider, observed: each request as sent, never edited."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: List[Dict[str, Any]] = []

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        self.requests.append({"messages": kwargs.get("messages")})
        return self.inner.call_single_step(**kwargs)

    def tool_trace(self) -> List[Dict[str, Any]]:
        """Each tool the model called: its arguments and what came back (titles only)."""
        if not self.requests:
            return []
        calls: Dict[str, Dict[str, Any]] = {}
        out: List[Dict[str, Any]] = []
        for message in self.requests[-1].get("messages") or []:
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls[str(block.get("id"))] = {"name": block.get("name"), "input": block.get("input")}
                elif block.get("type") == "tool_result":
                    content = block.get("content")
                    try:
                        body = json.loads(content) if isinstance(content, str) else {}
                    except ValueError:
                        body = {}
                    result = body.get("result") if isinstance(body.get("result"), dict) else {}
                    call = calls.get(str(block.get("tool_use_id")), {})
                    out.append({
                        "name": call.get("name"), "input": call.get("input"),
                        "status": result.get("status"), "found": result.get("found"),
                        "reason": result.get("reason"),
                        "products": [p.get("title") for p in result.get("products") or []][:5],
                        "sections": [s.get("title") for s in result.get("sections") or []][:5],
                    })
        return out


class ScriptedModel:
    """Local smoke only: one knowledge lookup, then a reply citing what came back."""

    def __init__(self) -> None:
        self.calls = 0

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        from core.commerce_runtime import agent_provider as ap

        self.calls += 1
        messages = kwargs.get("messages") or []
        results = [b for m in messages if m.get("role") == "user"
                   for b in (m.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_result"]

        def step(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
            return {"provider": "anthropic", "model": "scripted", "status": "ok", "stop_reason": "tool_use",
                    "blocks": blocks, "usage": {"input_tokens": 10, "output_tokens": 5}, "request_id": "r"}
        if not results:
            return step([{"type": "tool_use", "id": f"k{self.calls}", "name": "search_merchant_knowledge",
                          "input": {"query": "رسوم التوصيل الشحن"}}])
        body = json.loads(results[-1]["content"]) if isinstance(results[-1].get("content"), str) else {}
        refs = [s.get("evidence_ref") for s in (body.get("result") or {}).get("sections") or []
                if s.get("evidence_ref")]
        return step([{"type": "tool_use", "id": f"r{self.calls}", "name": ap.REPLY_TOOL_NAME,
                      "input": {"text": "معلومة من المتجر", "evidence_refs": refs,
                                "claims_commerce_facts": bool(refs)}}])


# ── One turn ─────────────────────────────────────────────────────────────────


class _Trace:
    def mark_outbound_sent(self, **_kw: Any) -> None:
        return None


def run_turn(ctx: "Context", store: Store, scenario: Scenario) -> Dict[str, Any]:
    from core.commerce_runtime import pilot_guard
    from core.commerce_runtime import runtime_entry as entry
    from core.conversation_engine import StateManager
    from models import Conversation
    from services import commerce_runtime_pilot as seam
    from sqlalchemy import text as sql

    name = NAMES[scenario.store]
    customer_id, conversation_id, phone = new_conversation(ctx.engine, store, name)
    db = ctx.session_factory()
    try:
        StateManager.save_message(db, phone, scenario.message, "inbound", conversation_id=conversation_id,
                                  tenant_id=store.tenant_id)
        db.commit()
        convo = db.get(Conversation, conversation_id)
        preamble = seam._context_preamble(db, store.tenant_id, convo, name)
        history = seam._prior_turns(db, tenant_id=store.tenant_id, conversation_id=conversation_id,
                                    phone=phone, current_text=scenario.message)
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
        connection_id=str(store.connection_id), customer_id=customer_id, normalized_customer_phone=phone,
        provider_message_id="wamid.eval.in." + uuid.uuid4().hex, inbound_text=scenario.message,
        inbound_metadata={}, transport=entry.whatsapp_reply_transport(
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
        convo = db.execute(sql("SELECT id FROM conversations WHERE id = :c"), {"c": conversation_id}).first()
        seam._record(db=db, trace=_Trace(), convo=convo, tenant_id=store.tenant_id, to=phone,
                     report=report, wire=wire)
        db.commit()
    finally:
        db.close()
    return {"report": report.as_log_fields(), "sent": last, "sends": len(senders.sent), "wall_ms": wall_ms,
            "tool_trace": provider.tool_trace()}


def measure(reply: str, store: Store, evidence_refs: str) -> Dict[str, Any]:
    """Structure only; the judgement of each claim is made by reading the texts."""
    numbers = {n.replace(",", ".") for n in NUMBER.findall(reply.translate(ARABIC_DIGITS))}
    return {
        "text_chars": len(reply),
        "numbers_in_text": sorted(numbers),
        "numbers_not_in_store": sorted(n for n in numbers if n not in store.numbers),
        "cites_knowledge": "kb:section:" in str(evidence_refs or ""),
    }


@dataclasses.dataclass
class Context:
    engine: Any
    session_factory: Any
    model: str
    stores: Dict[str, Store]


def run_scenario(ctx: Context, scenario: Scenario, rep: int) -> Dict[str, Any]:
    store = ctx.stores[scenario.store]
    base = {"label": LABEL, "scenario": scenario.name, "store": scenario.store, "rep": rep,
            "input": scenario.message}
    try:
        result = run_turn(ctx, store, scenario)
    except Exception as exc:  # noqa: BLE001 - one failed turn is a result, not the end
        record = {**base, "error": type(exc).__name__, "detail": str(exc)[:200]}
        emit("EVAL_TURN=", record)
        return record
    report = result["report"]
    reply = str((result.get("sent") or {}).get("text") or "")
    record = {
        **base, "reply_text": reply, "delivery": (result.get("sent") or {}).get("kind"),
        "sends": result["sends"], "tool_trace": result.get("tool_trace"), "wall_ms": result["wall_ms"],
        **{k: report.get(k) for k in ("reason", "stop_reason", "steps_used", "tool_calls_used",
                                      "tools_called", "evidence_refs", "input_tokens", "output_tokens",
                                      "latency_ms", "model")},
        "metrics": measure(reply, store, report.get("evidence_refs")),
    }
    emit("EVAL_TURN=", record)
    return record


def summarise(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by: Dict[str, List[Mapping[str, Any]]] = {}
    for record in records:
        if "metrics" in record:
            by.setdefault(record["scenario"], []).append(record)

    def med(values: List[Any]) -> Any:
        values = [v for v in values if isinstance(v, (int, float))]
        return statistics.median(values) if values else None

    return {name: {
        "n": len(items),
        "knowledge_found_turns": sum(1 for i in items if any(
            t.get("name") == "search_merchant_knowledge" and t.get("found") for t in i.get("tool_trace") or [])),
        "cites_knowledge_turns": sum(1 for i in items if i["metrics"]["cites_knowledge"]),
        "numbers_not_in_store_turns": sum(1 for i in items if i["metrics"]["numbers_not_in_store"]),
        "steps_median": med([i.get("steps_used") for i in items]),
        "latency_ms_median": med([i.get("latency_ms") for i in items]),
    } for name, items in sorted(by.items())}


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from budget import Budget
    try:
        budget = Budget.from_env()
    except Exception as exc:  # noqa: BLE001 - no cap, no run
        emit("EVAL_SUMMARY=", {"label": LABEL, "status": "refused", "reason": str(exc)[:200]})
        return 2
    admin = os.environ.get(ADMIN_ENV, "")
    model = os.environ.get(MODEL_ENV, "") if not SCRIPTED else "scripted-model"
    if not admin or not model or (not SCRIPTED and not (os.environ.get("ANTHROPIC_API_KEY")
                                                         or os.environ.get("CLAUDE_API_KEY"))):
        emit("EVAL_SUMMARY=", {"label": LABEL, "status": "usage",
                               "needs": [ADMIN_ENV, MODEL_ENV, "ANTHROPIC_API_KEY"]})
        return 2
    if not SCRIPTED:
        budget.add(model, 0, 0)      # refuses a model it cannot price, before any call
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
        stores = {"F": seed_store(engine, "F", CLOTHING, KNOWLEDGE_F),
                  "J": seed_store(engine, "J", PERFUME, KNOWLEDGE_J),
                  "A": seed_store(engine, "A", CLOTHING)}
        ctx = Context(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False),
                      model=model, stores=stores)
        wanted = [s.strip() for s in os.environ.get("EVAL_SCENARIOS", "").split(",") if s.strip()]
        scenarios = [s for s in SCENARIOS if not wanted or s.name in wanted]
        stopped = False
        for rep in range(1, REPEATS + 1):
            for scenario in scenarios:
                if budget.exhausted:
                    stopped = True
                    break
                record = run_scenario(ctx, scenario, rep)
                if "metrics" in record and not SCRIPTED:
                    budget.add(record.get("model") or model, record.get("input_tokens"),
                               record.get("output_tokens"))
                records.append(record)
        emit("EVAL_SUMMARY=", {"label": LABEL, "status": "budget_stop" if stopped else "done",
                               "spent_usd": round(budget.spent_usd, 4), "budget_usd": budget.limit_usd,
                               "model": model, "repeats": REPEATS,
                               "turns": sum(1 for r in records if "metrics" in r),
                               "errors": [r for r in records if "error" in r],
                               "by_scenario": summarise(records)})
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
