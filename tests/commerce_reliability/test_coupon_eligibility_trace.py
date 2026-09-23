"""The scoped read-only trace: what it proves, and what it must never print.

The runtime's ``[PROMOTION_PROJECTION]`` line deliberately carries no customer
identifier and no order counts — a general log is the wrong home for them. This
trace is the right home: scoped to one tenant and one conversation, run
deliberately, answering the four questions an empty coupon answer raises.

Two things are proved here. That it reads and never writes — asserted from what
the module's own code names, and from a real PostgreSQL run that leaves the
rows exactly as it found them. And that its output carries reasons, counts and
internal ids but never a coupon code, a phone number or a customer name.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Set

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCRIPT = REPO_ROOT / "scripts" / "operators" / "coupon_eligibility_trace.py"

TENANT = 991_777
PHONE = "+966500111222"
CODE_GENERAL = "EIDTRACE20"
CODE_UNEARNED = "SILVERTRACE15"     # a rung the store allows, this customer has not reached
CODE_NOT_ALLOWED = "GOLDTRACE50"    # a rung the store keeps away from the assistant
CUSTOMER_NAME = "نورة عبدالله"


def _identifiers(path: Path) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
    return names


def _sql_literals(path: Path) -> list[str]:
    """Every string the module hands to ``text(...)``."""
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "text" and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):  # pragma: no cover - none today
                out.append(ast.unparse(arg))
    return out


def test_every_statement_the_trace_runs_is_a_read() -> None:
    """Read-only is a property of the SQL, not of the reviewer's intention.
    A dict that happens to be called ``.update()`` is not a write; an UPDATE
    statement is, and there is none."""
    statements = _sql_literals(SCRIPT)
    assert statements, "no SQL found — the scan is looking in the wrong place"
    writes = ("insert ", "update ", "delete ", "drop ", "alter ", "truncate ",
              "create ", "grant ", "copy ")
    for sql in statements:
        lowered = " ".join(sql.lower().split()) + " "
        assert not any(verb in lowered for verb in writes), sql[:120]
        assert lowered.startswith(("select ", "show ")), sql[:120]


def test_the_trace_imports_no_issuance_path_and_uses_the_platforms_authorities() -> None:
    """The answers must be the platform's own, or they are a second opinion
    rather than evidence — and nothing here may issue a coupon."""
    names = _identifiers(SCRIPT)
    forbidden = {"issue_customer_coupon", "CouponGeneratorService", "pick_coupon_for_level",
                 "create_coupon", "generate_coupon", "find_reusable_assigned_coupon",
                 "register_evidence_rows", "commit", "flush"}
    assert not names & forbidden, f"writes or issues: {sorted(names & forbidden)}"
    assert {"resolve_level_entitlement", "count_customer_orders",
            "list_shareable_promotions_impl", "order_lookup_key"} <= names


def test_the_connection_is_held_read_only_by_postgres_itself() -> None:
    """Not a convention the script follows — a setting the server enforces."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "default_transaction_read_only=on" in source


@pytest.fixture()
def seeded(disposable_pg):
    """One merchant, one customer, one conversation, two coupons: a general one
    the merchant declared and a rung this customer has not earned."""
    engine = disposable_pg.engine
    dsn = disposable_pg.dsn
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM coupons WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM customers WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:t, :n) "
                          "ON CONFLICT (id) DO NOTHING"),
                     {"t": TENANT, "n": "متجر تجريبي عام"})
        customer_id = int(conn.execute(
            text("INSERT INTO customers (tenant_id, phone, normalized_phone, name) "
                 "VALUES (:t, :p, :p, :n) RETURNING id"),
            {"t": TENANT, "p": PHONE, "n": CUSTOMER_NAME}).scalar_one())
        conversation_id = int(conn.execute(
            text("INSERT INTO conversations (tenant_id, customer_id, external_id, status) "
                 "VALUES (:t, :c, :e, 'open') RETURNING id"),
            {"t": TENANT, "c": customer_id, "e": PHONE}).scalar_one())
        for code, level, meta in (
                (CODE_GENERAL, None, {"source": "dashboard", "ai_allocatable": False}),
                (CODE_UNEARNED, "silver", {"source": "dashboard"}),
                (CODE_NOT_ALLOWED, "gold", {"source": "dashboard"})):
            conn.execute(
                text("INSERT INTO coupons (tenant_id, code, description, discount_type, "
                     "discount_value, source_type, coupon_level, expires_at, metadata) "
                     "VALUES (:t, :c, 'خصم', 'percentage', '10', 'manual', :lv, "
                     "now() + interval '30 days', CAST(:m AS jsonb))"),
                {"t": TENANT, "c": code, "lv": level, "m": json.dumps(meta)})
    try:
        yield {"engine": engine, "dsn": dsn, "customer_id": customer_id,
               "conversation_id": conversation_id}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM coupons WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM customers WHERE tenant_id = :t"), {"t": TENANT})


def _run(seeded: Dict[str, Any], **extra: str) -> Dict[str, Any]:
    env = dict(os.environ)
    env.update({"DATABASE_URL": seeded["dsn"], "NAHLA_TRACE_TENANT_ID": str(TENANT),
                "NAHLA_TRACE_CONVERSATION_ID": str(seeded["conversation_id"])})
    env.update(extra)
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                          env=env, cwd=str(REPO_ROOT), timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("COUPON_TRACE="))
    return json.loads(line[len("COUPON_TRACE="):])


def test_the_trace_answers_all_four_questions_from_a_real_database(seeded) -> None:
    """«أريد تتبّعًا يثبت» — the record, the orders, the rung, and the reason.
    A general coupon the merchant declared is kept; the gold rung this customer
    never earned is withheld, and the count says which gate took it."""
    out = _run(seeded)
    assert out["read_only"] == "on"

    # 1. Which record the conversation is bound to.
    assert out["identity"]["conversation"]["customer_id"] == seeded["customer_id"]
    assert out["identity"]["resolved_customer_id"] == seeded["customer_id"]

    # 2. Orders read, counted, set aside — a searched history with nothing in it.
    assert out["orders"]["searched"] is True
    assert out["orders"]["order_lookup_key_present"] is True
    assert out["orders"]["raw_orders"] == 0 and out["orders"]["countable_orders"] == 0
    assert out["count_customer_orders"]["history_established"] is True

    # 3. The rung the merchant's policy resolved, and how firmly.
    assert out["entitlement"]["determined"] is True
    assert out["entitlement"]["reason"] == "no_entitled_level"
    assert out["entitlement"]["countable_orders"] == 0

    # 4. Which gate took which coupon — two different gates, named apart.
    assert out["projection"]["kept"] == 1
    assert out["projection"]["withheld"] == {"level_not_earned_by_customer": 1,
                                             "level_not_allowed_by_store_policy": 1}
    # And the rows a person can check the counts against.
    assert len(out["candidate_coupons"]) == 3
    assert {row["coupon_level"] for row in out["candidate_coupons"]} == {None, "silver", "gold"}


def test_a_phone_that_resolves_elsewhere_is_reported_as_a_disagreement(seeded) -> None:
    """The conversation's binding and the phone lookup are different routes.
    When they disagree the trace says so instead of picking one."""
    out = _run(seeded, NAHLA_TRACE_PHONE=PHONE)
    assert out["identity"]["phone"]["matched_records"] == 1
    assert out["identity"]["routes_agree"] is True
    # The number itself is never printed back.
    assert PHONE not in json.dumps(out, ensure_ascii=False)


def test_the_output_carries_no_code_no_phone_and_no_name(seeded) -> None:
    """A trace exists to explain a refusal, never to become a second way to
    read what was refused."""
    blob = json.dumps(_run(seeded, NAHLA_TRACE_PHONE=PHONE), ensure_ascii=False)
    for secret in (CODE_GENERAL, CODE_UNEARNED, CODE_NOT_ALLOWED,
                   PHONE, PHONE.lstrip("+"), CUSTOMER_NAME):
        assert secret not in blob, secret
    # The masked forms are what a reader gets instead.
    assert "EI" + "*" * (len(CODE_GENERAL) - 2) in blob


def test_the_trace_leaves_every_row_exactly_as_it_found_it(seeded) -> None:
    """Read-only, proved against the table rather than asserted about it."""
    engine = seeded["engine"]
    snapshot = "SELECT id, code, coupon_level, allocation_channel, metadata::text FROM coupons " \
               "WHERE tenant_id = :t ORDER BY id"
    with engine.connect() as conn:
        before = [tuple(r) for r in conn.execute(text(snapshot), {"t": TENANT})]
        customers_before = conn.execute(
            text("SELECT count(*) FROM customers WHERE tenant_id = :t"), {"t": TENANT}).scalar_one()
    _run(seeded, NAHLA_TRACE_PHONE=PHONE)
    with engine.connect() as conn:
        after = [tuple(r) for r in conn.execute(text(snapshot), {"t": TENANT})]
        customers_after = conn.execute(
            text("SELECT count(*) FROM customers WHERE tenant_id = :t"), {"t": TENANT}).scalar_one()
    assert after == before and customers_after == customers_before


def test_a_customer_whose_history_cannot_be_searched_is_reported_as_unsearched(seeded) -> None:
    """The distinction the owner asked for, end to end: a record with no phone
    the order index can be searched by reports zero counters *and* says the
    search never ran, so nobody reads it as "this customer has never bought"."""
    engine = seeded["engine"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE customers SET phone = '', normalized_phone = '' "
                          "WHERE tenant_id = :t AND id = :c"),
                     {"t": TENANT, "c": seeded["customer_id"]})
    out = _run(seeded)
    assert out["orders"]["order_lookup_key_present"] is False
    assert out["orders"]["searched"] is False
    assert out["orders"]["countable_orders"] is None
    assert out["count_customer_orders"]["history_established"] is False
    assert out["entitlement"]["determined"] is False
    assert out["entitlement"]["reason"] == "order_history_not_searchable"
