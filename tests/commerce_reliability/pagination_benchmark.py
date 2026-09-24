"""Measure what paging costs, through the real runtime, on real PostgreSQL.

Not a test: a reproducible measurement. It runs the same scripted turns the
pagination proofs run — the real ``run_commerce_runtime_turn`` with only the
model's HTTP call and the transport doubled — against two databases per
catalogue size: one at revision ``0111`` (paging off: exactly the production
shape today) and one at ``0113`` (paging on). For each it records:

* statements the turn issued and its wall time (median of ``--repeat`` runs);
* what the model was sent: the tool declarations, the reply declaration, the
  search result and the conversation-context block, in bytes;
* the platform's own reads: the candidate read and the list-row hydration.

The model is scripted, so wall time is the platform's cost alone — no model
latency is in it.

    NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://.../postgres \\
        python tests/commerce_reliability/pagination_benchmark.py --sizes 30 200 1000 5000
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import create_engine, event, text  # noqa: E402

from core.commerce_runtime import agent_live_tools as alt  # noqa: E402
from core.commerce_runtime import agent_provider as ap  # noqa: E402
from core.commerce_runtime import navigation as nav  # noqa: E402
from core.commerce_runtime import reply_choices as rc  # noqa: E402
from core.commerce_runtime import runtime_entry as entry  # noqa: E402
from tests.commerce_reliability import test_commerce_runtime_pagination_pg as harness  # noqa: E402
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (  # noqa: E402
    _alembic,
    _create_database,
    _drop_database,
)


def _seed(engine: Any, size: int) -> harness.Shop:
    shop = harness._seed(engine)
    with engine.begin() as conn:
        rows = [{"t": shop.tenant_a, "x": "SKU-B" + uuid.uuid4().hex[:10],
                 "ti": f"قميص قطني أزرق دفعة {n}", "p": 50 + n % 90,
                 "m": json.dumps({"image_url": "https://cdn.example.test/x.jpg",
                                  "product_url": "https://shop.example.test/x"})}
                for n in range(max(0, size - harness.COUNTS[harness.SHIRTS]))]
        if rows:
            conn.execute(text(
                "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
                "stock_quantity, metadata) VALUES (:t, :x, :ti, 'منتج عام', :p, true, 5, "
                "CAST(:m AS JSONB))"), rows)
        conn.execute(text("ANALYZE products"))
    return shop


def _turn(shop: harness.Shop, conversation: int, model: Any, metadata: Dict[str, Any] = None,
          question: str = "Show me") -> Dict[str, Any]:
    statements: List[str] = []
    started = time.perf_counter()
    report, transport = shop.turn(conversation, model, statements=statements,
                                  metadata=metadata, question=question)
    elapsed = (time.perf_counter() - started) * 1000
    return {"report": report, "transport": transport, "statements": len(statements),
            "ms": elapsed, "model": model}


def _model_bytes(model: Any) -> Dict[str, int]:
    first = model.calls[0]
    tools = first["tools"]
    reply = next(t for t in tools if t["name"] == ap.REPLY_TOOL_NAME)
    others = [t for t in tools if t["name"] != ap.REPLY_TOOL_NAME]
    out = {"tool_declarations": len(json.dumps(others, ensure_ascii=False).encode()),
           "reply_declaration": len(json.dumps(reply, ensure_ascii=False).encode()),
           "input_all_steps": sum(len(json.dumps({"m": c["messages"], "t": c["tools"]},
                                                 ensure_ascii=False).encode())
                                  for c in model.calls)}
    try:
        out["search_result"] = len(json.dumps(model.search_result(), ensure_ascii=False).encode())
    except AssertionError:
        out["search_result"] = 0
    try:
        out["context_block"] = len(model.context_block().encode())
    except StopIteration:
        out["context_block"] = 0
    return out


def measure(admin_dsn: str, size: int, repeat: int, paging: bool) -> Dict[str, Any]:
    name, dsn = _create_database(admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, "0111")
        if paging:
            _alembic(dsn, "0113")
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        shop = _seed(engine, size)
        opening, more_turns, plain = [], [], []
        last_open = last_more = last_plain = None
        for _ in range(repeat):
            conversation = shop.conversation()
            last_open = _turn(shop, conversation, harness.browse(harness.SHIRTS))
            opening.append(last_open)
            rows, _button = rc.payload_rows(last_open["transport"].sent[0])
            more = [r for r in rows if nav.is_navigation_row(r)]
            if more:
                last_more = _turn(shop, conversation, harness.answer(),
                                  metadata={"list_reply_id": more[0]["id"],
                                            "list_reply_title": more[0]["title"]},
                                  question=more[0]["title"])
                more_turns.append(last_more)
            last_plain = _turn(shop, shop.conversation(), harness.answer("Hello."))
            plain.append(last_plain)

        def med(runs: List[Dict[str, Any]], key: str) -> Any:
            return round(statistics.median(r[key] for r in runs), 1) if runs else None

        return {
            "size": size, "paging": paging,
            "opening_turn": {"statements": med(opening, "statements"), "ms": med(opening, "ms"),
                             "rows_sent": last_open["report"].choice_rows,
                             "browse": last_open["report"].browse_outcome,
                             "model_bytes": _model_bytes(last_open["model"])},
            "more_turn": ({"statements": med(more_turns, "statements"), "ms": med(more_turns, "ms"),
                           "rows_sent": last_more["report"].choice_rows,
                           "model_bytes": _model_bytes(last_more["model"])}
                          if more_turns else None),
            "plain_text_turn": {"statements": med(plain, "statements"), "ms": med(plain, "ms"),
                                "model_bytes": _model_bytes(last_plain["model"])},
        }
    finally:
        entry.reset_schema_probe()
        nav.reset_schema_probe()
        engine.dispose()
        _drop_database(admin_dsn, name)


def platform_reads(admin_dsn: str, size: int, repeat: int) -> Dict[str, Any]:
    """The two reads paging adds, timed on their own at this catalogue size."""
    from sqlalchemy.orm import sessionmaker

    from core.store_knowledge import CatalogContextBuilder

    name, dsn = _create_database(admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, "0111")
        shop = _seed(engine, size)
        session = sessionmaker(bind=engine)()
        builder = CatalogContextBuilder(session, shop.tenant_a)
        statements: List[str] = []
        event.listen(engine, "before_cursor_execute", lambda *a: statements.append(a[2]))

        def timed(work):
            runs = []
            for _ in range(repeat):
                statements.clear()
                started = time.perf_counter()
                result = work()
                runs.append(((time.perf_counter() - started) * 1000, len(statements)))
            return result, round(statistics.median(r[0] for r in runs), 2), runs[-1][1]

        _, row_ms, row_q = timed(lambda: builder.search_products(
            harness.SHIRTS, limit=5, include_non_orderable_facts=True))
        cands, cand_ms, cand_q = timed(lambda: builder.search_product_candidates(harness.SHIRTS, 50))
        _, top_ms, top_q = timed(lambda: builder.top_product_candidates(50))
        ids = list(cands.product_ids[9:18])
        _, hyd_ms, hyd_q = timed(lambda: builder.get_by_ids(ids))
        session.close()
        return {"size": size,
                "model_window_search_5": {"ms": row_ms, "statements": row_q},
                "candidate_read_50": {"ms": cand_ms, "statements": cand_q,
                                      "ids": len(cands.product_ids), "complete": cands.exhausted},
                "general_browse_candidates_50": {"ms": top_ms, "statements": top_q},
                "list_row_hydration_9": {"ms": hyd_ms, "statements": hyd_q}}
    finally:
        engine.dispose()
        _drop_database(admin_dsn, name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[30, 200, 1000, 5000])
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    admin = os.environ["NAHLA_RELIABILITY_PG_ADMIN_DSN"]
    out = {"turns": [], "reads": []}
    for size in args.sizes:
        for paging in (False, True):
            out["turns"].append(measure(admin, size, args.repeat, paging))
        out["reads"].append(platform_reads(admin, size, max(args.repeat, 10)))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
