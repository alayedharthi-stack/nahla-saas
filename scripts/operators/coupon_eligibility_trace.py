#!/usr/bin/env python3
"""Read-only trace: why this customer did or did not receive a coupon.

Answers, for one tenant and one conversation, the four questions an empty
coupon answer raises — and answers them by running the platform's own
authorities against the live row, not by re-deriving them here:

1. **Which customer record.** The runtime takes its customer from the
   conversation row; the trusted-context shadow resolves its own from the
   phone. Both are reported, because they can disagree and the disagreement is
   itself a finding.
2. **How many orders were read, counted and set aside, and why.** The count
   comes from ``count_customer_orders`` — the same call the entitlement makes —
   and the breakdown groups the customer's own orders by the status the
   countability policy classified them under.
3. **Which rung the merchant's policy resolved**, from
   ``resolve_level_entitlement``, with its reason and how firmly it is known.
4. **Which gate withheld each coupon**, from ``list_shareable_promotions_impl``
   itself: the per-reason ``withheld`` counts, plus one masked line per
   currently-valid coupon showing the fields each gate reads.

Why a script and not a log line: the runtime's ``[PROMOTION_PROJECTION]`` line
deliberately carries no customer identifier and no order counts, because a
general log is the wrong place for them. A scoped trace, run once against one
conversation, is the right place — so this exists instead of widening that log.

Nothing is written. Every statement runs in a session PostgreSQL itself holds
read-only (``default_transaction_read_only=on``), and the tool call is the
platform's read-only implementation, which imports no issuance path.

What is printed: internal integer ids (the answer to question 1 *is* an id),
counts, statuses, reasons and policy values. Never a coupon code, a phone
number, a customer name, an address, or any credential. Coupon codes appear as
``AB****`` with their length.

Usage
─────
    DATABASE_URL=...                       # the verified application database
    NAHLA_TRACE_TENANT_ID=1
    NAHLA_TRACE_CONVERSATION_ID=9          # the application conversation id
    python scripts/operators/coupon_eligibility_trace.py

``NAHLA_TRACE_PHONE`` may be given instead of, or beside, the conversation id:
it is normalised by the platform's own helper, used to find the customer
record, and never printed. ``NAHLA_TRACE_CUSTOMER_ID`` pins the customer
directly when both other routes are ambiguous.

Output: one ``COUPON_TRACE=`` JSON line. Exit 0 when the trace completed,
1 when it could not run (nothing was proved either way).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "backend"), str(ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _mask_code(code: Any) -> str:
    text_code = str(code or "")
    if not text_code:
        return ""
    return text_code[:2] + "*" * max(len(text_code) - 2, 0)


def _env_int(name: str) -> Optional[int]:
    raw = str(os.environ.get(name, "") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class _ReadOnlyScope:
    """Exactly the surface ``list_shareable_promotions_impl`` touches.

    A shim, and named as one: the gates it exercises — the promotion-truth
    resolver, the entitlement read, the projection and its withheld counts —
    are the platform's real ones, which is what makes the result evidence.
    ``register_evidence`` collects rather than persists, because a trace must
    not leave a mark on the conversation it is tracing.
    """

    def __init__(self, db: Any, tenant_id: int, customer_id: Optional[int]) -> None:
        self.db = db
        self.tenant_id = int(tenant_id)
        self.customer_id = int(customer_id) if customer_id else None
        self.evidence: List[Any] = []

    def assert_scope(self) -> None:
        if self.tenant_id <= 0:
            raise ValueError("trace_scope_requires_a_tenant")

    def register_evidence(self, records: Any) -> None:
        self.evidence.extend(records or ())


def _identity(conn: Any, tenant_id: int) -> Dict[str, Any]:
    """Which customer record each route arrives at, and whether they agree."""
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    out: Dict[str, Any] = {}
    conversation_id = _env_int("NAHLA_TRACE_CONVERSATION_ID")
    pinned = _env_int("NAHLA_TRACE_CUSTOMER_ID")
    phone = str(os.environ.get("NAHLA_TRACE_PHONE", "") or "").strip()

    from_conversation = None
    if conversation_id:
        row = conn.execute(
            text("SELECT customer_id, status FROM conversations "
                 "WHERE tenant_id = :t AND id = :c"),
            {"t": tenant_id, "c": conversation_id}).mappings().first()
        out["conversation"] = {"id": conversation_id, "found": row is not None,
                               "customer_id": (row or {}).get("customer_id"),
                               "status": (row or {}).get("status")}
        from_conversation = (row or {}).get("customer_id")

    from_phone: List[int] = []
    if phone:
        normalized = normalize_phone(phone)
        out["phone"] = {"normalized_non_empty": bool(normalized),
                        "digits": len(normalized or "")}
        if normalized:
            rows = conn.execute(
                text("SELECT id FROM customers WHERE tenant_id = :t "
                     "AND (phone = :p OR normalized_phone = :p) ORDER BY id"),
                {"t": tenant_id, "p": normalized}).mappings().all()
            from_phone = [int(r["id"]) for r in rows]
            out["phone"]["customer_ids"] = from_phone
            out["phone"]["matched_records"] = len(from_phone)

    resolved = pinned or from_conversation or (from_phone[0] if from_phone else None)
    out["pinned_customer_id"] = pinned
    out["resolved_customer_id"] = int(resolved) if resolved else None
    # The runtime uses the conversation's customer. A phone that resolves
    # somewhere else means the conversation is bound to a different record,
    # which is a finding about the binding and not about the customer.
    out["routes_agree"] = (
        from_conversation is not None and bool(from_phone)
        and int(from_conversation) in from_phone
    ) if (from_conversation is not None and from_phone) else None
    return out


def _customer_record(conn: Any, tenant_id: int, customer_id: int) -> Dict[str, Any]:
    row = conn.execute(
        text("SELECT id, (coalesce(phone, '') <> '') AS has_phone, "
             "(coalesce(normalized_phone, '') <> '') AS has_normalized_phone "
             "FROM customers WHERE tenant_id = :t AND id = :c"),
        {"t": tenant_id, "c": customer_id}).mappings().first()
    return dict(row) if row else {"found": False}


def _orders(conn: Any, tenant_id: int, customer_id: int) -> Dict[str, Any]:
    """What the index returned for this customer, grouped by how the
    countability policy classified it. The counts themselves come from the
    platform's own ``count_customer_orders`` below; this is the *why*."""
    from services.order_countability_policy import (  # noqa: PLC0415
        EXCLUDED_ORDER_STATUSES, is_countable_order, order_status_key,
    )
    from models import Customer, Order  # noqa: PLC0415
    from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

    with Session(conn) as session:
        customer = (session.query(Customer)
                    .filter(Customer.tenant_id == tenant_id, Customer.id == customer_id)
                    .first())
        if customer is None:
            return {"customer_found": False}
        intel = CustomerIntelligenceService(session, tenant_id)
        lookup_key = intel.order_lookup_key(customer)
        if not lookup_key:
            # No phone the order index can be searched by: zero here would be
            # the absence of a search, not a history of no purchases.
            return {"customer_found": True, "order_lookup_key_present": False,
                    "searched": False, "raw_orders": None, "countable_orders": None}
        rows = intel._orders_for_customer(customer)
        by_status: Dict[str, Dict[str, Any]] = {}
        for order in rows:
            status = order_status_key(order) or "(blank)"
            entry = by_status.setdefault(status, {"orders": 0, "countable": 0, "excluded": 0})
            entry["orders"] += 1
            if is_countable_order(order):
                entry["countable"] += 1
            else:
                entry["excluded"] += 1
                entry["excluded_because"] = (
                    "status_excluded_by_policy" if status in EXCLUDED_ORDER_STATUSES
                    else "abandoned_flag" if bool(getattr(order, "is_abandoned", False))
                    else "status_not_recognised_as_countable")
        tenant_total = session.query(Order).filter(Order.tenant_id == tenant_id).count()
        return {
            "customer_found": True,
            "order_lookup_key_present": True,
            "searched": True,
            "raw_orders": len(rows),
            "countable_orders": sum(1 for o in rows if is_countable_order(o)),
            "excluded_orders": sum(1 for o in rows if not is_countable_order(o)),
            "by_status": by_status,
            # Scope, stated: only this merchant's orders are ever in the index.
            "tenant_orders_total": tenant_total,
            "note": "orders are scoped to this tenant; another merchant's "
                    "purchases are never counted here",
        }


def _entitlement(engine: Any, tenant_id: int, customer_id: Optional[int]) -> Dict[str, Any]:
    from services.coupon_entitlement_read import resolve_level_entitlement  # noqa: PLC0415
    from services.customer_request_coupon_service import count_customer_orders  # noqa: PLC0415

    with Session(engine) as session:
        out: Dict[str, Any] = {}
        if customer_id:
            count = count_customer_orders(session, tenant_id, customer_id)
            out["count_customer_orders"] = (
                {"result": None} if count is None else
                {"raw_orders": count.raw_orders, "countable_orders": count.countable_orders,
                 "excluded_orders": count.excluded_orders, "count_source": count.count_source,
                 "history_established": count.history_established})
        out["entitlement"] = resolve_level_entitlement(session, tenant_id, customer_id).as_dict()
        return out


def _merchant_policy(engine: Any, tenant_id: int) -> Dict[str, Any]:
    from services.coupon_generator import _get_ai_policy, _get_coupon_dashboard_block  # noqa: PLC0415
    from services.customer_request_coupon_service import (  # noqa: PLC0415
        _first_purchase_rule_from_dashboard,
    )

    with Session(engine) as session:
        out: Dict[str, Any] = {}
        try:
            out["ai_policy"] = dict(_get_ai_policy(session, tenant_id))
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            out["ai_policy"] = {"unreadable": type(exc).__name__}
        try:
            block = _get_coupon_dashboard_block(session, tenant_id)
            levels = block.get("levels")
            out["levels"] = [
                {"id": entry.get("id"), "enabled": entry.get("enabled", True),
                 "min_orders": entry.get("min_orders")}
                for entry in (levels if isinstance(levels, list) else [])
                if isinstance(entry, dict)]
            rule = _first_purchase_rule_from_dashboard(block)
            out["first_purchase_rule"] = dict(rule) if isinstance(rule, dict) else rule
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            out["levels"] = {"unreadable": type(exc).__name__}
        return out


def _projection(engine: Any, tenant_id: int, customer_id: Optional[int]) -> Dict[str, Any]:
    """The tool the agent actually calls, run read-only for this customer."""
    import asyncio  # noqa: PLC0415

    from modules.ai.commerce_agent_v2.tools.promotions import (  # noqa: PLC0415
        list_shareable_promotions_impl,
    )

    with Session(engine) as session:
        scope = _ReadOnlyScope(session, tenant_id, customer_id)
        try:
            result = asyncio.run(list_shareable_promotions_impl(scope))
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            return {"unreadable": type(exc).__name__, "detail": str(exc)[:200]}
        return {
            "status": getattr(result, "status", ""),
            "query_outcome": getattr(result, "query_outcome", ""),
            "failure_reason": getattr(result, "failure_reason", None),
            "partial": bool(getattr(result, "partial", False)),
            "kept": len(getattr(result, "promotions", None) or ()),
            "withheld": dict(getattr(result, "withheld", None) or {}),
            "entitlement": dict(getattr(result, "entitlement", None) or {}),
            "kept_codes_masked": [_mask_code(getattr(p, "code", ""))
                                  for p in (getattr(result, "promotions", None) or ())],
        }


def _candidate_rows(conn: Any, tenant_id: int) -> List[Dict[str, Any]]:
    """One masked line per currently-valid coupon, carrying only the fields the
    gates read. This is what makes a withheld count checkable by a person."""
    rows = conn.execute(
        text("SELECT id, length(code) AS code_len, "
             "left(code, 2) AS code_prefix, coupon_level, allocation_channel, source_type, "
             "expires_at, (metadata->>'source') AS meta_source, "
             "(metadata->>'ai_allocatable') AS meta_ai_allocatable, "
             "(metadata ? 'customer_id' OR metadata ? 'assigned_customer_id' "
             " OR metadata ? 'owner_customer_id') AS customer_bound "
             "FROM coupons WHERE tenant_id = :t "
             "AND (expires_at IS NULL OR expires_at > now()) "
             "ORDER BY id LIMIT 200"),
        {"t": tenant_id}).mappings().all()
    out: List[Dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["code_masked"] = (str(record.pop("code_prefix", "") or "")
                                 + "*" * max(int(record.get("code_len") or 0) - 2, 0))
        out.append(record)
    return out


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("COUPON_TRACE=" + json.dumps({"error": "DATABASE_URL is not set"}), flush=True)
        return 1
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    tenant_id = _env_int("NAHLA_TRACE_TENANT_ID")
    if not tenant_id:
        print("COUPON_TRACE=" + json.dumps({"error": "NAHLA_TRACE_TENANT_ID is not set"}), flush=True)
        return 1

    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"options": "-c default_transaction_read_only=on"})
    out: Dict[str, Any] = {"tenant_id": tenant_id}
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        out["read_only"] = conn.execute(text("SHOW default_transaction_read_only")).scalar()
        out["db_now"] = str(conn.execute(text("SELECT now()")).scalar())
        try:
            out["alembic_heads"] = [r[0] for r in
                                    conn.execute(text("SELECT version_num FROM alembic_version"))]
        except Exception as exc:  # noqa: BLE001 - a schema without the table is said, not fatal
            out["alembic_heads"] = {"unreadable": type(exc).__name__}

        out["identity"] = _identity(conn, tenant_id)
        customer_id = out["identity"].get("resolved_customer_id")
        if customer_id:
            out["customer_record"] = _customer_record(conn, tenant_id, int(customer_id))
            out["orders"] = _orders(conn, tenant_id, int(customer_id))
        out["candidate_coupons"] = _candidate_rows(conn, tenant_id)

    out.update(_entitlement(engine, tenant_id, customer_id))
    out["merchant_policy"] = _merchant_policy(engine, tenant_id)
    out["projection"] = _projection(engine, tenant_id, customer_id)
    print("COUPON_TRACE=" + json.dumps(out, default=str, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
