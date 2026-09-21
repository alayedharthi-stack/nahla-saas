#!/usr/bin/env python3
"""Isolated synthetic probe of the Commerce Runtime pilot.

Real runtime, real read tools, real PostgreSQL, a real model when one is
configured — and no WhatsApp, no webhook, no production rows. This is the
reviewable, re-runnable form of the probe that previously lived only inside a
one-off service's environment variable.

What it does, in order
──────────────────────
1. Creates a disposable database ``nahla_synthetic_probe_<hex>`` on the server
   named by ``NAHLA_SYNTHETIC_PROBE_ADMIN_DSN`` and migrates it with the
   repository's own chain to revision ``0111``. The database the DSN itself
   names is used for ``CREATE DATABASE`` / ``DROP DATABASE`` only.
2. Seeds one generic merchant in that database: four products, each with a
   store link and an image link; one shipping knowledge section; one shareable
   coupon and one campaign-only coupon; one customer, one conversation and one
   WhatsApp connection. Nothing of any real merchant is read.
3. Runs each case through ``run_commerce_runtime_turn`` — the pilot's own entry
   point, with the pilot's own instructions, budget and read tools — using the
   platform's Anthropic provider on the model named by
   ``COMMERCE_RUNTIME_PILOT_MODEL`` (the key is read by the provider from
   ``ANTHROPIC_API_KEY`` and never printed), or ``--provider scripted`` for a
   model-free self-test with deterministic answers. The transport is simulated:
   it runs the platform's wire sanitiser on the reserved text and records what
   would have reached the customer.
4. Replays every inbound once, with the same provider message id, and records
   that it was not answered twice.
5. Prints one ``SYNTHETIC_RESULT=`` JSON line per case and one
   ``SYNTHETIC_SUMMARY=`` line, then drops the database — also on failure.

No secret is ever printed: the output names environment variables, never
their values, and every line is scrubbed against the values of the variables
this process was given.

Usage
─────
    NAHLA_SYNTHETIC_PROBE_ADMIN_DSN=postgresql://<user>:<password>@<host>:<port>/postgres \\
    COMMERCE_RUNTIME_PILOT_MODEL=<the approved model id> \\
    ANTHROPIC_API_KEY=<key> \\
    python scripts/operators/commerce_runtime_synthetic_probe.py

    python scripts/operators/commerce_runtime_synthetic_probe.py --provider scripted   # no model needed

Exit codes: 0 every case ran and met its expectations; 1 a case missed an
expectation; 2 usage or environment; 3 the probe itself failed.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

LOG_PREFIX = "[COMMERCE_RUNTIME_SYNTHETIC_PROBE]"
RESULT_PREFIX = "SYNTHETIC_RESULT="
SUMMARY_PREFIX = "SYNTHETIC_SUMMARY="
ADMIN_DSN_ENV = "NAHLA_SYNTHETIC_PROBE_ADMIN_DSN"
MODEL_ENV = "COMMERCE_RUNTIME_PILOT_MODEL"
KEY_ENV = "ANTHROPIC_API_KEY"
REVISION = "0111"
PHONE = "+966500000001"                       # a synthetic handset; never a real customer
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_SCRIPTED = "scripted"

EXIT_OK, EXIT_EXPECTATION, EXIT_USAGE, EXIT_FAILED = 0, 1, 2, 3

logger = logging.getLogger("nahla.commerce_runtime.synthetic_probe")


# ── Cases ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Case:
    """One synthetic customer turn and what the runtime is expected to do with it.

    Expectations describe the runtime, not the model's wording: which tools
    must have run, whether the reply must cite evidence, whether the wire text
    must equal the reserved text, and that nothing was sent twice.
    """

    name: str
    question: str
    expect_tools: Tuple[str, ...] = ()        # each must appear in tools_called
    expect_reply: bool = True                 # an accepted send is expected
    expect_evidence: bool = False             # at least one evidence ref cited
    expect_links: bool = False                # the wire text carries store links, unchanged
    expect_coupon_evidence: bool = False      # a promotion:coupon:* ref cited


CASES: Tuple[Case, ...] = (
    Case("greeting", "السلام عليكم"),
    Case("listing_with_links", "ابي اشوف المنتجات المتوفرة مع الروابط والصور",
         expect_tools=("search_products",), expect_evidence=True, expect_links=True),
    Case("details_bundle", "ابي تفاصيل وصور كل الأحذية عندكم",
         expect_tools=("search_products",), expect_evidence=True),
    Case("coupon", "عندكم كود خصم؟",
         expect_tools=("list_shareable_promotions",), expect_evidence=True,
         expect_coupon_evidence=True),
    Case("unsupported_write", "ألغ طلبي ورجع فلوسي الآن", expect_reply=True),
)


# ── Seed ─────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Seed:
    tenant_id: int
    connection_id: int
    customer_id: int
    conversation_id: int
    product_ids: Tuple[int, ...]
    section_id: int
    coupon_id: int
    campaign_coupon_id: int
    store_host: str


PRODUCTS: Tuple[Tuple[str, str, int, int], ...] = (
    ("حذاء رياضي أبيض", "حذاء رياضي قطني مريح", 199, 4),
    ("حذاء جلد بني", "حذاء جلد طبيعي للمناسبات", 349, 2),
    ("حذاء أطفال أزرق", "حذاء أطفال خفيف بإغلاق لاصق", 120, 6),
    ("حذاء رياضي أسود", "حذاء رياضي بنعل مرن", 219, 3),
)


def seed_merchant(engine: Any) -> Seed:
    """One generic merchant, in the disposable database only."""
    from sqlalchemy import text

    store_host = "demo-probe.example-store.sa"
    with engine.begin() as conn:
        tenant_id = int(conn.execute(
            text("INSERT INTO tenants (name, is_active, is_platform_tenant) "
                 "VALUES (:n, true, false) RETURNING id"),
            {"n": "متجر تجريبي عام " + uuid.uuid4().hex[:6]}).scalar_one())
        connection_id = int(conn.execute(
            text("INSERT INTO whatsapp_connections (tenant_id, phone_number_id, status) "
                 "VALUES (:t, :p, 'connected') RETURNING id"),
            {"t": tenant_id, "p": "1555" + uuid.uuid4().hex[:8]}).scalar_one())
        customer_id = int(conn.execute(
            text("INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                 "VALUES (:t, :p, :p, :n) RETURNING id"),
            {"t": tenant_id, "p": PHONE, "n": "نورة عبدالله"}).scalar_one())
        conversation_id = int(conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, :c, :e, 'active') RETURNING id"),
            {"t": tenant_id, "c": customer_id, "e": PHONE}).scalar_one())
        product_ids: List[int] = []
        for index, (title, description, price, qty) in enumerate(PRODUCTS, start=1):
            external_id = f"PROBE-{index}-{uuid.uuid4().hex[:6]}"
            # The catalog builder reads a product's store link and image link
            # from its sync metadata, exactly as a synced Salla/Zid row carries them.
            product_ids.append(int(conn.execute(
                text("INSERT INTO products (tenant_id, external_id, title, description, price, "
                     "in_stock, stock_quantity, metadata) "
                     "VALUES (:t, :x, :ti, :d, :pr, true, :q, CAST(:meta AS jsonb)) RETURNING id"),
                {"t": tenant_id, "x": external_id, "ti": title, "d": description, "pr": price,
                 "q": qty,
                 "meta": json.dumps({"product_url": f"https://{store_host}/products/{external_id}",
                                     "image_url": f"https://{store_host}/images/{external_id}.jpg"})},
            ).scalar_one()))
        section_id = int(conn.execute(
            text("INSERT INTO merchant_knowledge_sections (tenant_id, kind, title, body, priority, "
                 "is_active, source, ai_status, created_at, updated_at) "
                 "VALUES (:t, 'shipping', :ti, :b, 10, true, 'manual', 'approved', now(), now()) "
                 "RETURNING id"),
            {"t": tenant_id, "ti": "التوصيل",
             "b": "التوصيل داخل المملكة خلال ثلاثة إلى خمسة أيام عمل، ورسوم التوصيل 25 ريال."},
        ).scalar_one())
        coupon_id = int(conn.execute(
            text("INSERT INTO coupons (tenant_id, code, description, discount_type, discount_value, "
                 "source_type, allocation_channel) "
                 "VALUES (:t, 'WELCOME10', 'خصم ترحيبي على أول طلب', 'percentage', '10', 'manual', NULL) "
                 "RETURNING id"), {"t": tenant_id}).scalar_one())
        campaign_coupon_id = int(conn.execute(
            text("INSERT INTO coupons (tenant_id, code, description, discount_type, discount_value, "
                 "source_type, allocation_channel) "
                 "VALUES (:t, 'EMAILONLY', 'حملة بريدية', 'percentage', '25', 'manual', 'campaign') "
                 "RETURNING id"), {"t": tenant_id}).scalar_one())
    return Seed(tenant_id=tenant_id, connection_id=connection_id, customer_id=customer_id,
                conversation_id=conversation_id, product_ids=tuple(product_ids),
                section_id=section_id, coupon_id=coupon_id,
                campaign_coupon_id=campaign_coupon_id, store_host=store_host)


# ── Disposable database ──────────────────────────────────────────────────────


def create_database(admin_dsn: str) -> Tuple[str, str]:
    from sqlalchemy import create_engine, text

    name = "nahla_synthetic_probe_" + uuid.uuid4().hex[:10]
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE \"{name}\" ENCODING 'UTF8' TEMPLATE template0"))
    finally:
        admin.dispose()
    return name, admin_dsn.rsplit("/", 1)[0] + "/" + name


def drop_database(admin_dsn: str, name: str) -> None:
    from sqlalchemy import create_engine, text

    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def alembic_config(dsn: str) -> Any:
    """The migration configuration, built in code rather than read from
    ``alembic.ini``. The ini carries a logging section that ``env.py`` installs
    with ``fileConfig`` whenever a config file is named, and that call disables
    every logger created before it — the sanitiser's among them when the probe
    runs in a process that has already imported it. The probe measures that
    logger, so its migration leaves the process's logging alone."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(APP_ROOT / "database" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def migrate(dsn: str, revision: str = REVISION) -> None:
    """Run the repository's own migration chain against the disposable database."""
    from alembic import command

    cfg = alembic_config(dsn)
    previous_cwd, previous_url = os.getcwd(), os.environ.get("DATABASE_URL")
    os.chdir(APP_ROOT / "database")
    os.environ["DATABASE_URL"] = dsn
    try:
        command.upgrade(cfg, revision)
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


# ── Simulated transport and wire observation ─────────────────────────────────


@dataclasses.dataclass
class SimulatedTransport:
    """Accepts every send with a synthetic id after running the platform's own
    wire sanitiser on the text — the guard ``_post_wa`` runs — and records
    what would have reached the customer. Nothing leaves this process."""

    tenant_id: int
    case: str
    calls: int = 0
    wire: List[str] = dataclasses.field(default_factory=list)
    sanitised: List[bool] = dataclasses.field(default_factory=list)

    def __call__(self, payload: Mapping[str, Any]) -> Any:
        from core.commerce_runtime import ledger_contracts as lc
        from core.outbound_sanitizer import sanitize_outbound_payload

        self.calls += 1
        body = str(payload.get("text") or "")
        wire_payload = {"messaging_product": "whatsapp", "to": PHONE, "type": "text",
                        "text": {"body": body}}
        out, was_sanitised = sanitize_outbound_payload(wire_payload, tenant_id=self.tenant_id,
                                                       skip_handoff_scrub=True)
        self.wire.append(str(out["text"]["body"]))
        self.sanitised.append(bool(was_sanitised))
        return lc.SendResponse(http_status=200,
                               body={"messages": [{"id": f"synthetic-{self.case}-{self.calls}"}]})


SANITIZER_LOGGER = "nahla.security.outbound_sanitizer"


class SanitizerLogCapture(logging.Handler):
    """Collects the sanitiser's audit and block lines for one case."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.audit: List[str] = []
        self.blocked: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "[OUTBOUND_URL_AUDIT]" in message:
            self.audit.append(message)
        if "[EXTERNAL_RESEARCH_BLOCKED]" in message:
            self.blocked.append(message)


@contextlib.contextmanager
def capturing_sanitizer_log() -> Iterator[SanitizerLogCapture]:
    """The sanitiser's audit and block lines for one case, whatever the
    process's logging configuration did to that logger before the case: the
    audit line is INFO and the logger may sit at WARNING, or a ``fileConfig``
    elsewhere in the process may have disabled it, and either would make the
    probe report a silent sanitiser that did speak. Level, enablement and the
    handler are restored afterwards."""
    capture = SanitizerLogCapture()
    logger = logging.getLogger(SANITIZER_LOGGER)
    previous_level, previously_disabled = logger.level, logger.disabled
    logger.setLevel(logging.INFO)
    logger.disabled = False
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.disabled = previously_disabled


# ── Scripted provider (model-free self-test) ─────────────────────────────────


def _step(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"provider": "anthropic", "model": "scripted-model", "status": "ok",
            "stop_reason": "tool_use", "blocks": blocks,
            "usage": {"input_tokens": 100, "output_tokens": 20}, "request_id": "req"}


def _tool_use(call_id: str, name: str, **arguments: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def _reply(text_body: str, refs: Sequence[str] = (), *, commerce: bool) -> Dict[str, Any]:
    from core.commerce_runtime import agent_provider as ap

    return {"type": "tool_use", "id": "reply", "name": ap.REPLY_TOOL_NAME,
            "input": {"text": text_body, "evidence_refs": list(refs), "claims_commerce_facts": commerce}}


def scripted_answers(case: Case, seed: Seed) -> List[Dict[str, Any]]:
    """Deterministic answers that exercise the same runtime paths a model would."""
    refs = [f"catalog:product:{pid}" for pid in seed.product_ids]
    if case.name == "greeting":
        return [_step([_reply("وعليكم السلام، أهلاً بك. كيف أقدر أساعدك؟", commerce=False)])]
    if case.name == "listing_with_links":
        listing = "عندنا أربعة أحذية متوفرة:\n" + "\n".join(
            f"{i + 1}) {title} — https://{seed.store_host}/products/p{pid} — الصورة: "
            f"https://{seed.store_host}/images/p{pid}.jpg"
            for i, (pid, (title, *_)) in enumerate(zip(seed.product_ids, PRODUCTS)))
        return [_step([_tool_use("t1", "search_products", query="حذاء", limit=5)]),
                _step([_reply(listing, refs, commerce=True)])]
    if case.name == "details_bundle":
        return [_step([_tool_use("t1", "search_products", query="حذاء", limit=5)]),
                _step([_tool_use(f"d{i}", "get_product_details", product_id=pid)
                       for i, pid in enumerate(seed.product_ids)]),
                _step([_reply("هذي تفاصيل الأحذية الأربعة مع صورها.", refs, commerce=True)])]
    if case.name == "coupon":
        return [_step([_tool_use("p1", "list_shareable_promotions")]),
                _step([_reply("عندنا كود WELCOME10 خصم 10% على أول طلب.",
                              [f"promotion:coupon:{seed.coupon_id}"], commerce=True)])]
    if case.name == "unsupported_write":
        # No order was found, so the reply states no commerce fact and cites nothing:
        # a reply that claimed one without evidence would be refused by verification.
        return [_step([_tool_use("o1", "resolve_customer_order", purpose="status")]),
                _step([_reply("ما لقيت طلبًا باسمك، وما أقدر ألغي أو أرجّع مبلغًا من هنا.", commerce=False)])]
    raise ValueError(f"no scripted answers for case {case.name!r}")


class ScriptedAnthropic:
    """Stands in for the model's HTTP call only; the real adapter runs above it."""

    def __init__(self, answers: List[Dict[str, Any]]) -> None:
        self._answers = list(answers)

    def call_single_step(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._answers:
            return {"provider": "anthropic", "model": "scripted", "status": "sdk_error",
                    "stop_reason": None, "blocks": [], "usage": None, "error": "script_exhausted"}
        return self._answers.pop(0)


# ── Running one case ─────────────────────────────────────────────────────────


def run_case(*, engine: Any, session_factory: Any, seed: Seed, case: Case, provider: str,
             model: str, instructions: str, budget: Any) -> Dict[str, Any]:
    from core.commerce_runtime import runtime_entry as entry
    from core.outbound_sanitizer import url_hosts

    provider_message_id = f"wamid.synthetic.{case.name}.{uuid.uuid4().hex}"
    transport = SimulatedTransport(tenant_id=seed.tenant_id, case=case.name)
    anthropic_provider = ScriptedAnthropic(scripted_answers(case, seed)) if provider == PROVIDER_SCRIPTED else None

    def turn(transport_: Any) -> Any:
        return entry.run_commerce_runtime_turn(
            engine=engine, session_factory=session_factory,
            tenant_id=seed.tenant_id, conversation_id=seed.conversation_id,
            connection_ref=f"wa:{seed.connection_id}", connection_id=str(seed.connection_id),
            customer_id=seed.customer_id, normalized_customer_phone=PHONE,
            provider_message_id=provider_message_id, inbound_text=case.question,
            inbound_metadata={"source": "synthetic_probe", "case": case.name},
            transport=transport_, instructions=instructions, model=model, budget=budget,
            context_preamble={"channel": "whatsapp", "verified_customer_name": "نورة عبدالله"},
            anthropic_provider=anthropic_provider,
        )

    with capturing_sanitizer_log() as capture:
        report = turn(transport)
        # The same inbound again, exactly as a provider retry would deliver it.
        replay_transport = SimulatedTransport(tenant_id=seed.tenant_id, case=case.name + "-replay")
        replay = turn(replay_transport)

    fields = report.as_log_fields()
    fields["reply_text"] = report.reply_text
    wire_text = transport.wire[0] if transport.wire else ""
    result: Dict[str, Any] = {
        **fields,
        "case": case.name,
        "question": case.question,
        "provider": provider,
        "transport_calls": transport.calls,
        "transport_is_simulated": True,
        "wire_text": wire_text,
        "wire_equals_reserved_intent": bool(transport.wire) and wire_text == report.reply_text,
        "wire_sanitised": bool(transport.sanitised and transport.sanitised[0]),
        "wire_hosts": url_hosts(wire_text),
        "sanitizer_audit_lines": len(capture.audit),
        "sanitizer_blocked_lines": len(capture.blocked),
        "replay_reason": replay.reason,
        "replay_transport_calls": replay_transport.calls,
    }
    result["expectations"] = evaluate(case, result)
    return result


def evaluate(case: Case, result: Mapping[str, Any]) -> Dict[str, bool]:
    tools = str(result.get("tools_called") or "").split(",") if result.get("tools_called") else []
    refs = str(result.get("evidence_refs") or "").split(",") if result.get("evidence_refs") else []
    checks = {
        "answered_once": (bool(result.get("replied")) == case.expect_reply)
                         and int(result.get("transport_calls") or 0) <= 1,
        "not_answered_twice": int(result.get("replay_transport_calls") or 0) == 0,
        "expected_tools_ran": all(tool in tools for tool in case.expect_tools),
        "nothing_blocked_on_the_wire": int(result.get("sanitizer_blocked_lines") or 0) == 0,
    }
    if case.expect_evidence:
        checks["evidence_cited"] = bool(refs)
    if case.expect_links:
        checks["links_reached_the_wire_unchanged"] = (
            bool(result.get("wire_equals_reserved_intent")) and bool(result.get("wire_hosts")))
    if case.expect_coupon_evidence:
        checks["coupon_evidence_cited"] = any(ref.startswith("promotion:coupon:") for ref in refs)
    return checks


# ── Output hygiene ───────────────────────────────────────────────────────────


def secret_values() -> List[str]:
    """Values of every environment variable that could be a secret, longest first."""
    values: List[str] = []
    for key, value in os.environ.items():
        upper = key.upper()
        if any(token in upper for token in ("KEY", "SECRET", "TOKEN", "PASSWORD", "DSN", "DATABASE_URL")):
            if value and len(value) >= 6:
                values.append(value)
    return sorted(set(values), key=len, reverse=True)


def scrub(text: str, secrets: Sequence[str]) -> str:
    for value in secrets:
        text = text.replace(value, "<redacted>")
    return re.sub(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@", r"\1<redacted>@", text)


def emit(prefix: str, payload: Mapping[str, Any], secrets: Sequence[str]) -> None:
    line = prefix + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    print(scrub(line, secrets), flush=True)


# ── Main ─────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", choices=(PROVIDER_ANTHROPIC, PROVIDER_SCRIPTED),
                        default=PROVIDER_ANTHROPIC)
    parser.add_argument("--cases", default=",".join(c.name for c in CASES),
                        help="comma-separated case names (default: all)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    secrets = secret_values()
    admin_dsn = os.environ.get(ADMIN_DSN_ENV, "").strip()
    if not admin_dsn:
        print(f"{LOG_PREFIX} usage: set {ADMIN_DSN_ENV} to an admin DSN whose server may hold a "
              f"disposable database", file=sys.stderr)
        return EXIT_USAGE
    if args.provider == PROVIDER_ANTHROPIC:
        model = os.environ.get(MODEL_ENV, "").strip()
        if not model:
            print(f"{LOG_PREFIX} usage: {MODEL_ENV} must name the approved model; the probe never "
                  f"chooses one", file=sys.stderr)
            return EXIT_USAGE
        if not os.environ.get(KEY_ENV, "").strip():
            print(f"{LOG_PREFIX} usage: {KEY_ENV} is not set; use --provider scripted for a "
                  f"model-free self-test", file=sys.stderr)
            return EXIT_USAGE
    else:
        model = "scripted-model"
    wanted = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in {c.name for c in CASES}]
    if unknown:
        print(f"{LOG_PREFIX} usage: unknown cases {unknown}", file=sys.stderr)
        return EXIT_USAGE
    cases = [c for c in CASES if c.name in wanted]

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from core.commerce_runtime import pilot_guard
    from modules.ai.commerce_agent_v2.pilot_instructions import build_pilot_instructions

    name, dsn = create_database(admin_dsn)
    print(f"{LOG_PREFIX} database={name} revision={REVISION} provider={args.provider} "
          f"model={model} cases={len(cases)}", flush=True)
    engine = None
    exit_code = EXIT_OK
    try:
        migrate(dsn, REVISION)
        engine = create_engine(dsn, future=True)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        seed = seed_merchant(engine)
        instructions = build_pilot_instructions()
        budget = pilot_guard.pilot_budget()
        results: List[Dict[str, Any]] = []
        for case in cases:
            result = run_case(engine=engine, session_factory=session_factory, seed=seed, case=case,
                              provider=args.provider, model=model, instructions=instructions,
                              budget=budget)
            results.append(result)
            emit(RESULT_PREFIX, result, secrets)
        with engine.connect() as conn:
            effects = int(conn.execute(
                text("SELECT count(*) FROM commerce_runtime_effects WHERE tenant_id = :t"),
                {"t": seed.tenant_id}).scalar_one())
        duplicate_sends = sum(
            max(0, int(r["transport_calls"]) - 1) + int(r["replay_transport_calls"]) for r in results)
        unmet = {r["case"]: [k for k, ok in r["expectations"].items() if not ok] for r in results}
        unmet = {case: keys for case, keys in unmet.items() if keys}
        summary = {
            "cases": len(results), "model": model, "provider": args.provider,
            "real_model": args.provider == PROVIDER_ANTHROPIC,
            "real_runtime_and_tools": True, "real_postgresql": True, "real_webhook": False,
            "real_whatsapp_transport": False, "production_customer_rows_used": False,
            "duplicate_sends": duplicate_sends, "commerce_effects": effects,
            "unmet_expectations": unmet,
        }
        emit(SUMMARY_PREFIX, summary, secrets)
        if unmet or duplicate_sends or effects:
            exit_code = EXIT_EXPECTATION
    except Exception as exc:  # noqa: BLE001 - the verdict is the exit code; the database is dropped either way
        print(scrub(f"{LOG_PREFIX} failed error={type(exc).__name__}: {exc}", secrets), file=sys.stderr)
        exit_code = EXIT_FAILED
    finally:
        if engine is not None:
            engine.dispose()
        try:
            drop_database(admin_dsn, name)
            print(f"SYNTHETIC_DATABASE_REMOVED={name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(scrub(f"{LOG_PREFIX} could not drop database={name} error={type(exc).__name__}",
                        secrets), file=sys.stderr)
            exit_code = EXIT_FAILED if exit_code == EXIT_OK else exit_code
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
