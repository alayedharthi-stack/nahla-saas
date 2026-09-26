"""The promotion tool reads the codes it could keep first, not the newest rows.

A real-PostgreSQL proof of the read behind ``list_shareable_promotions``.

The resolver it calls judged only the tenant's newest ``limit * 3`` coupon rows
(24 for the tool), and the tool then refused rows on rung and remaining life.
A store with thousands of synced codes — expired ones, campaign codes, codes on
rungs the assistant is not allowed to serve — could therefore hide every code
it would accept, and the tool reported ``not_found``: an empty answer nobody
had established. Production logged exactly that window full on 2026-09-25
(``candidate_count=24 shareable=8`` then ``considered=8 kept=0`` for a gold
customer served silver, in a tenant holding 3262 coupons). Whether a qualifying
silver code sat behind that window then cannot be read from the logs; what is
proved here is the mechanism, and its removal.

The data is a generic merchant, not any production store. Nothing here issues,
assigns or generates a coupon.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce import promotion_truth as pt  # noqa: E402
from modules.ai.commerce_agent_v2.tools import promotions as tool  # noqa: E402
from services import coupon_entitlement_read as cer  # noqa: E402

TENANT = 991_812
CUSTOMER = 4_812
OTHER_CUSTOMER = 4_813
# The shipped AI policy: bronze and silver only, three hours of life at least.
POLICY: Dict[str, Any] = {"enabled": True, "allowed_levels": ["bronze", "silver"],
                          "min_remaining_hours": 3, "pool_mode": "pool_first"}


class Context:
    """The trusted context as the tool sees it, on a real session."""

    def __init__(self, db: Any) -> None:
        self.tenant_id = TENANT
        self.customer_id = CUSTOMER
        self.db = db
        self.registered: List[Any] = []

    def assert_scope(self) -> None:
        return None

    def register_evidence(self, records: List[Any]) -> None:
        self.registered.extend(records)


def _shipped_ladder() -> Any:
    """The ladder exactly as the dashboard ships it: gold is kept to campaigns."""
    from backend.routers.coupons import DEFAULT_COUPON_LEVELS, _normalise_levels

    return _normalise_levels(DEFAULT_COUPON_LEVELS)


def _gold(customer_id: int = CUSTOMER) -> cer.LevelEntitlement:
    return cer.LevelEntitlement(customer_id=customer_id, countable_orders=7, resolved_level="gold",
                                entitled_levels=("gold",), reason=cer.REASON_ENTITLED)


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def store(disposable_pg):
    engine = disposable_pg.engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM coupons WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:t, :n) ON CONFLICT (id) DO NOTHING"),
                     {"t": TENANT, "n": "متجر تجريبي عام — كوبونات"})
    session = disposable_pg.session_factory()
    try:
        yield engine, session
    finally:
        session.close()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM coupons WHERE tenant_id = :t"), {"t": TENANT})


def _insert(engine: Any, rows: Iterable[Dict[str, Any]], *, tenant: int = TENANT) -> None:
    """Rows in the order given, so a later row has a higher id: newer."""
    with engine.begin() as conn:
        for row in rows:
            conn.execute(
                text("INSERT INTO coupons (tenant_id, code, description, discount_type, discount_value, "
                     "source_type, coupon_level, allocation_channel, expires_at, metadata) "
                     "VALUES (:t, :code, 'خصم', 'percentage', '10', :source, :level, :channel, :expires, "
                     "CAST(:meta AS jsonb))"),
                {"t": tenant, "code": row["code"], "source": row.get("source", "system"),
                 "level": row.get("level"), "channel": row.get("channel", "shared"),
                 "expires": row.get("expires"), "meta": json.dumps(row.get("meta") or {})})


def _run_tool(monkeypatch: pytest.MonkeyPatch, session: Any, *, policy: Optional[Dict[str, Any]] = None,
              ladder: Any = None) -> Any:
    import services.coupon_generator as generator

    monkeypatch.setattr(generator, "_get_ai_policy", lambda db, tid: dict(policy or POLICY))
    monkeypatch.setattr(generator, "_get_coupon_dashboard_block",
                        lambda db, tid: {"levels": _shipped_ladder() if ladder is None else ladder})
    monkeypatch.setattr(tool, "resolve_level_entitlement", lambda db, tid, cid: _gold(cid))
    return asyncio.run(tool.list_shareable_promotions_impl(Context(session), limit=tool.MAX_PROMOTIONS))


def _crowded_store(engine: Any, now: datetime) -> None:
    """One qualifying silver code, then everything a busy store adds after it:
    another customer's silver codes, gold codes the assistant may not serve,
    silver codes about to expire, campaign codes and expired imports."""
    later = _naive(now + timedelta(days=5))
    rows: List[Dict[str, Any]] = [{"code": "SILVER-KEEP", "level": "silver", "expires": later}]
    rows += [{"code": f"SILVER-THEIRS-{i}", "level": "silver", "expires": later,
              "meta": {"customer_id": OTHER_CUSTOMER}} for i in range(6)]
    rows += [{"code": f"GOLD-{i}", "level": "gold", "expires": later} for i in range(10)]
    rows += [{"code": f"SILVER-SOON-{i}", "level": "silver", "expires": _naive(now + timedelta(hours=1))}
             for i in range(10)]
    rows += [{"code": f"CAMPAIGN-{i}", "level": "silver", "channel": "campaign", "expires": later}
             for i in range(5)]
    rows += [{"code": f"IMPORTED-{i}", "source": "imported", "channel": None,
              "expires": _naive(now - timedelta(days=1))} for i in range(20)]
    _insert(engine, rows)


def test_a_qualifying_code_behind_newer_rows_it_would_refuse_is_found(store, monkeypatch) -> None:
    engine, session = store
    now = datetime.now(timezone.utc)
    _crowded_store(engine, now)

    # The mechanism, on the resolver's unscoped read: the window is spent on the
    # newest rows, and the one code this customer could be given is not in it.
    unscoped = pt.resolve_shareable_promotions(session, TENANT, limit=tool.MAX_PROMOTIONS,
                                               customer_id=CUSTOMER)
    assert unscoped.candidate_count == tool.MAX_PROMOTIONS * 3
    assert "SILVER-KEEP" not in [fact["code"] for fact in unscoped.shareable]

    result = _run_tool(monkeypatch, session)
    assert result.status == "ok"
    assert [p.code for p in result.promotions] == ["SILVER-KEEP"]
    # The standing and the rung served stay distinct: a gold customer is
    # offered a silver code because the store keeps gold from the assistant.
    assert result.entitlement["resolved_level"] == "gold"
    assert result.entitlement["served_level"] == "silver"
    # Another customer's codes are still refused, by the resolver, before the
    # projection sees them. The rest of the window is still read after the
    # code that qualified, so the gate that took each other code is named.
    assert not any(p.code.startswith("SILVER-THEIRS") for p in result.promotions)
    assert result.withheld == {tool.WITHHELD_EXPIRING_TOO_SOON: 7}


def test_when_nothing_qualifies_the_empty_answer_is_one_the_store_really_gave(store, monkeypatch) -> None:
    """No silver code with enough life, however many other codes exist: an
    honest ``not_found`` carrying the standing and the rung that was looked for."""
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    _insert(engine, [{"code": f"GOLD-{i}", "level": "gold", "expires": later} for i in range(10)]
            + [{"code": f"SILVER-SOON-{i}", "level": "silver",
                "expires": _naive(now + timedelta(hours=1))} for i in range(3)])
    result = _run_tool(monkeypatch, session)
    assert result.status == "not_found" and result.failure_reason == "no_valid_shareable_promotions"
    assert result.promotions == []
    # The same aggregate shape production logged. It is what a store with
    # nothing to give produces, and — before this change — also what a store
    # hiding a code behind its newest rows produced; the log alone cannot say
    # which one 2026-09-25 was.
    assert result.withheld == {tool.WITHHELD_EXPIRING_TOO_SOON: 3, tool.WITHHELD_LEVEL_NOT_ALLOWED: 5}
    assert result.entitlement["resolved_level"] == "gold"
    assert result.entitlement["served_level"] == "silver"


def test_the_scoped_read_order_is_the_head_of_a_full_read_under_the_same_rules(store) -> None:
    """``read_first`` changes the order rows are read in, never how a row is
    judged: with no ``accept``, its answer starts with exactly what a read of
    every row, filtered by the rules the scope stands for, holds first. (The
    tool's own judgement, ``accept``, is proved by the cases above.)"""
    engine, session = store
    now = datetime.now(timezone.utc)
    _crowded_store(engine, now)
    later = _naive(now + timedelta(days=9))
    # Codes on no rung, and silver codes older than the crowd, interleaved.
    _insert(engine, [{"code": f"OPEN-{i}", "source": "manual", "expires": later} for i in range(6)]
            + [{"code": f"SILVER-OLD-{i}", "level": " Silver ", "expires": None} for i in range(2)])
    edge = now + timedelta(hours=3)

    scoped = pt.resolve_shareable_promotions(
        session, TENANT, limit=tool.MAX_PROMOTIONS, customer_id=CUSTOMER,
        read_first=pt.CouponReadScope(valid_until_at_least=edge,
                                      refused_levels=("bronze", "gold", "vip")))
    everything = pt.resolve_shareable_promotions(session, TENANT, limit=100_000, customer_id=CUSTOMER)
    assert everything.candidate_count < 100_000 * 3, "the reference read must see every row"

    def acceptable(fact: Dict[str, Any]) -> bool:
        expires = fact["expires_at"]
        if expires:
            moment = datetime.fromisoformat(expires)
            moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
            if moment < edge:
                return False
        return fact["coupon_level"].strip() in ("", "silver")

    expected = [fact["code"] for fact in everything.shareable if acceptable(fact)][:tool.MAX_PROMOTIONS]
    # Nine rows qualify, so the cut is exercised as well as the order.
    assert len(expected) == tool.MAX_PROMOTIONS
    assert [fact["code"] for fact in scoped.shareable] == expected

    # With fewer qualifying rows than room, they still come first and the rest
    # of the window follows, newest first, judged exactly as before.
    few = pt.resolve_shareable_promotions(
        session, TENANT, limit=tool.MAX_PROMOTIONS, customer_id=CUSTOMER,
        read_first=pt.CouponReadScope(valid_until_at_least=edge,
                                      refused_levels=("silver", "gold", "vip")))
    wanted = [fact["code"] for fact in everything.shareable
              if acceptable(fact) and not fact["coupon_level"].strip()]
    assert [fact["code"] for fact in few.shareable][:len(wanted)] == wanted
    rest = [fact["code"] for fact in everything.shareable if fact["code"] not in wanted]
    assert [fact["code"] for fact in few.shareable][len(wanted):] == rest[:tool.MAX_PROMOTIONS - len(wanted)]


def test_an_unreadable_ladder_still_meets_the_codes_it_could_not_judge(store, monkeypatch) -> None:
    """A rung scope is only applied when the ladder was read whole. When it was
    not, the rung-conditioned codes are still read, so the list is reported as
    short because of us, never as the store's own empty answer."""
    engine, session = store
    now = datetime.now(timezone.utc)
    _insert(engine, [{"code": f"SILVER-{i}", "level": "silver",
                      "expires": _naive(now + timedelta(days=5))} for i in range(2)])
    result = _run_tool(monkeypatch, session, ladder="not a ladder")
    assert result.status == "error"
    assert result.failure_reason == "coupon_level_policy_unreadable:not_a_ladder"
    assert result.withheld == {tool.WITHHELD_LEVEL_POLICY_UNREADABLE: 2}


def test_other_customers_codes_filling_the_read_do_not_hide_this_customers_code(store, monkeypatch) -> None:
    """More of another customer's silver codes than one page holds, all newer
    than the code this customer may be given. They are refused one by one and
    the read goes on past them; the answer is the qualifying code, not
    ``not_found``."""
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    _insert(engine, [{"code": "SILVER-KEEP", "level": "silver", "expires": later}]
            + [{"code": f"SILVER-THEIRS-{i}", "level": "silver", "expires": later,
                "meta": {"customer_id": OTHER_CUSTOMER}} for i in range(250)])
    result = _run_tool(monkeypatch, session)
    assert result.status == "ok"
    assert [p.code for p in result.promotions] == ["SILVER-KEEP"]


def test_codes_the_projection_refuses_do_not_end_the_read_at_eight(store, monkeypatch) -> None:
    """Newer codes inside the scope that the tool then refuses — here codes on
    no rung the merchant created but never published to the assistant — must
    not use up the answer: the read continues until it has codes the tool
    keeps, and the refusals are still counted by their reason."""
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    _insert(engine, [{"code": "SILVER-KEEP", "level": "silver", "expires": later}]
            + [{"code": f"MANUAL-OPEN-{i}", "source": "manual", "channel": None, "expires": later}
               for i in range(12)])
    result = _run_tool(monkeypatch, session)
    assert result.status == "ok"
    assert [p.code for p in result.promotions] == ["SILVER-KEEP"]
    assert result.withheld == {tool.WITHHELD_NOT_PUBLISHED: 12}


def test_a_read_cut_short_by_its_cap_says_so_instead_of_answering_none(store, monkeypatch) -> None:
    """The scan is bounded per call. When the bound ends it before a full list
    or the end of the scope, the answer is an incomplete read — never
    ``not_found``, which would claim the store has nothing."""
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    monkeypatch.setattr(pt, "_READ_FIRST_PAGE", 10)
    monkeypatch.setattr(pt, "_READ_FIRST_SCAN_CAP", 30)
    _insert(engine, [{"code": "SILVER-KEEP", "level": "silver", "expires": later}]
            + [{"code": f"MANUAL-OPEN-{i}", "source": "manual", "channel": None, "expires": later}
               for i in range(40)])
    result = _run_tool(monkeypatch, session)
    assert result.status == "error" and result.partial is True
    assert result.failure_reason == tool.PROMOTION_READ_INCOMPLETE
    assert result.promotions == []
    assert result.withheld == {tool.WITHHELD_NOT_PUBLISHED: 30}
    # The same store, read to its end, answers with the code.
    monkeypatch.setattr(pt, "_READ_FIRST_SCAN_CAP", 1000)
    assert [p.code for p in _run_tool(monkeypatch, session).promotions] == ["SILVER-KEEP"]



class _Counting:
    """Every statement the engine runs while the block is open."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.statements: List[str] = []

    def _listen(self, _conn, _cursor, statement, _params, _context, _executemany) -> None:
        self.statements.append(statement)

    def __enter__(self) -> "_Counting":
        from sqlalchemy import event

        event.listen(self.engine, "before_cursor_execute", self._listen)
        return self

    def __exit__(self, *_exc: Any) -> None:
        from sqlalchemy import event

        event.remove(self.engine, "before_cursor_execute", self._listen)


def test_a_store_full_of_synced_imports_still_finds_the_code_in_a_few_queries(store, monkeypatch) -> None:
    """Realistic volume, cap untouched: 1,500 valid codes imported from the
    store platform, none on a rung and none the merchant published to the
    assistant, all newer than the one code this customer can be given. They
    are outside the scope, so the read neither stops on them nor loads them
    one query at a time."""
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    _insert(engine, [{"code": "SILVER-KEEP", "level": "silver", "expires": later}]
            + [{"code": f"SALLA-{i}", "source": "imported", "channel": None, "expires": later}
               for i in range(1500)])
    with _Counting(engine) as counted:
        result = _run_tool(monkeypatch, session)
    assert result.status == "ok" and [p.code for p in result.promotions] == ["SILVER-KEEP"]
    coupon_reads = [s for s in counted.statements if "FROM coupons" in s or "FROM coupon_rules" in s]
    assert len(coupon_reads) < 12, len(coupon_reads)


def test_a_store_full_of_imports_with_nothing_for_this_customer_answers_none(store, monkeypatch) -> None:
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    _insert(engine, [{"code": f"SALLA-{i}", "source": "imported", "channel": None, "expires": later}
                     for i in range(1200)]
            + [{"code": f"GOLD-{i}", "level": "gold", "expires": later} for i in range(3)])
    result = _run_tool(monkeypatch, session)
    assert result.status == "not_found" and result.failure_reason == "no_valid_shareable_promotions"


def test_a_scope_exactly_the_size_of_the_cap_read_to_its_end_is_complete(store, monkeypatch) -> None:
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    monkeypatch.setattr(pt, "_READ_FIRST_PAGE", 10)
    monkeypatch.setattr(pt, "_READ_FIRST_SCAN_CAP", 30)
    # Created by the merchant, on no rung, never published to the assistant:
    # in scope, and refused row by row.
    _insert(engine, [{"code": f"MANUAL-{i}", "source": "manual", "channel": None, "expires": later}
                     for i in range(30)])
    result = _run_tool(monkeypatch, session)
    assert result.status == "not_found"
    assert result.withheld == {tool.WITHHELD_NOT_PUBLISHED: 30}


def test_a_rung_padded_with_whitespace_is_still_read(store, monkeypatch) -> None:
    """The tool strips a rung before judging it; the scope must not lose a
    code the tool would accept because SQL and Python trim differently."""
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    _insert(engine, [{"code": "SILVER-TAB", "level": "silver\t", "expires": later}]
            + [{"code": f"GOLD-{i}", "level": "gold", "expires": later} for i in range(30)])
    result = _run_tool(monkeypatch, session)
    assert [p.code for p in result.promotions] == ["SILVER-TAB"]


def test_another_tenants_newer_codes_neither_hide_nor_leak(store, monkeypatch) -> None:
    engine, session = store
    now = datetime.now(timezone.utc)
    later = _naive(now + timedelta(days=5))
    other = TENANT + 1
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:t, :n) ON CONFLICT (id) DO NOTHING"),
                     {"t": other, "n": "متجر تجريبي عام — آخر"})
    try:
        _insert(engine, [{"code": "SILVER-KEEP", "level": "silver", "expires": later}])
        _insert(engine, [{"code": f"OTHER-SILVER-{i}", "level": "silver", "expires": later}
                         for i in range(300)], tenant=other)
        result = _run_tool(monkeypatch, session)
        assert [p.code for p in result.promotions] == ["SILVER-KEEP"]
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM coupons WHERE tenant_id = :t"), {"t": other})
