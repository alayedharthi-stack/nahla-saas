"""
verify_wa_draft_p0_live.py
──────────────────────────
Live / staging verification for P0 WhatsApp draft-order fix.

Runs a multi-turn cart + catalog + draft-confirmation simulation against
a real tenant DB (requires ``--tenant``), then checks:

  1. Draft order upserted via nahla_order_bridge
  2. Non-empty outbound reply injected (draft confirmation)
  3. order line_items have match_status / product_id evidence
  4. No unsafe free-text confirmed items

Also audits recent WA draft orders: every draft sync should have a
subsequent outbound MessageEvent on the same conversation.

Usage:
    cd backend
    python scripts/verify_wa_draft_p0_live.py --tenant 33
    python scripts/verify_wa_draft_p0_live.py --tenant 33 --audit-only
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (str(BACKEND), str(ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _db_session():
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set.")
    engine = create_engine(url)
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _find_or_create_conversation(db, tenant_id: int, phone: str = "966551309999"):
    from models import Conversation, Customer  # noqa: PLC0415

    cust = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.phone == phone)
        .first()
    )
    if cust is None:
        cust = Customer(tenant_id=tenant_id, phone=phone, normalized_phone=phone, name="P0 Live Test")
        db.add(cust)
        db.flush()

    conv = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id, Conversation.customer_id == cust.id)
        .order_by(Conversation.id.desc())
        .first()
    )
    if conv is None:
        conv = Conversation(tenant_id=tenant_id, customer_id=cust.id, extra_metadata={})
        db.add(conv)
        db.flush()
    return cust, conv


class _OutboundRecorder:
    """Tracks outbound replies that must follow each draft sync."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def record(self, *, conversation_id: int, body: str, trigger: str) -> None:
        if not (body or "").strip():
            raise RuntimeError(f"silent draft sync | trigger={trigger} conv={conversation_id}")
        self.events.append({
            "conversation_id": conversation_id,
            "direction": "outbound",
            "body": body.strip(),
            "trigger": trigger,
        })


def _simulate_turn(
    db,
    *,
    tenant_id: int,
    conversation: Any,
    customer: Any,
    message: str,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
    recorder: Optional["_OutboundRecorder"] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any], bool]:
    """Simulate cart apply + catalog resolve + draft confirmation (operational path)."""
    from modules.ai.brain.types import MerchantConversationState, OrderPreparationState  # noqa: PLC0415
    from modules.ai.brain.commerce.cart_state import maybe_apply_cart_message  # noqa: PLC0415
    from core.wa_cart_catalog_resolver import resolve_and_enrich_cart_state  # noqa: PLC0415
    from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: PLC0415
    from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

    state = MerchantConversationState.from_dict(brain_state) if brain_state else MerchantConversationState()
    prep = OrderPreparationState.from_dict(order_prep) if order_prep else OrderPreparationState()
    state.stage = state.stage or "ordering"

    cart_before = list(getattr(state, "cart_items", None) or [])
    maybe_apply_cart_message(
        state=state,
        prep=prep,
        message=message,
        product_info=state.current_product_focus,
    )
    cart_after = list(getattr(state, "cart_items", None) or [])
    cart_changed = cart_before != cart_after or bool(getattr(prep, "cart_deltas", None))

    catalog_resolution = None
    if cart_after:
        catalog_resolution = resolve_and_enrich_cart_state(db, tenant_id, state, prep)

    reply = maybe_inject_draft_flow_reply(
        reply="",
        order_prep=prep,
        brain_state=state,
        catalog_resolution=catalog_resolution,
        cart_changed=cart_changed,
    )

    bs = state.to_dict()
    op = bs.get("order_prep") or {}
    order = sync_nahla_wa_order(
        db,
        tenant_id=tenant_id,
        conversation=conversation,
        brain_state=bs,
        order_prep=op,
        trigger="p0_live_verify",
        customer=customer,
    )
    db.commit()

    if order is not None and recorder is not None:
        recorder.record(
            conversation_id=int(getattr(conversation, "id", 0) or 0),
            body=reply,
            trigger=f"draft_sync:{message[:24]}",
        )

    return reply or "", bs, op, order is not None


def _print_line_items(items: List[Dict[str, Any]]) -> None:
    if not items:
        print("    (no line_items)")
        return
    for i, item in enumerate(items, 1):
        print(
            f"    [{i}] name={item.get('product_name')!r} "
            f"product_id={item.get('product_id')!r} "
            f"variant={item.get('variant')!r} "
            f"variant_id={item.get('variant_id')!r} "
            f"qty={item.get('quantity')} "
            f"match_status={item.get('match_status')!r}"
        )


def _audit_recent_drafts(db, tenant_id: int, hours: int = 72) -> Tuple[int, int, List[str]]:
    """Return (draft_count, silent_count, details)."""
    from sqlalchemy import text  # noqa: PLC0415

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.execute(
        text(
            """
            SELECT o.id, o.conversation_id, o.external_id, o.status,
                   o.extra_metadata->>'draft_created_at' AS draft_at,
                   o.line_items
              FROM orders o
             WHERE o.tenant_id = :tid
               AND o.extra_metadata->>'origin' = 'whatsapp_ai'
               AND o.status IN ('draft', 'pending_customer_info', 'pending_payment')
               AND (
                    (o.extra_metadata->>'draft_created_at')::timestamptz >= :since
                    OR o.updated_at >= :since
               )
             ORDER BY o.id DESC
             LIMIT 30
            """
        ),
        {"tid": tenant_id, "since": since},
    ).fetchall()

    silent_details: List[str] = []
    silent = 0
    for row in rows:
        oid, conv_id, ext_id, status, draft_at, _ = row
        if not conv_id:
            continue
        outbound = db.execute(
            text(
                """
                SELECT id, created_at, left(coalesce(body,''), 120) AS preview
                  FROM message_events
                 WHERE tenant_id = :tid
                   AND conversation_id = :cid
                   AND direction = 'outbound'
                   AND created_at >= COALESCE((:draft_at)::timestamptz, NOW() - INTERVAL '5 minutes')
                 ORDER BY id ASC
                 LIMIT 3
                """
            ),
            {"tid": tenant_id, "cid": conv_id, "draft_at": draft_at},
        ).fetchall()
        if not outbound:
            silent += 1
            silent_details.append(
                f"order_id={oid} conv={conv_id} status={status} ext={ext_id} — NO outbound after draft"
            )
    return len(rows), silent, silent_details


def run_scenario(db, tenant_id: int, recorder: _OutboundRecorder) -> int:
    print(f"\n{BOLD}== P0 Live Simulation | tenant={tenant_id} =={RESET}\n")
    os.environ.setdefault("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
    os.environ.setdefault("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS", str(tenant_id))

    customer, conv = _find_or_create_conversation(db, tenant_id)
    brain_state: Dict[str, Any] = {"stage": "ordering", "cart_items": []}
    order_prep: Dict[str, Any] = {}
    failures = 0

    scenarios = [
        ("أحتاج عسل طلح أسود", "scenario1-talh"),
        ("كبير", "scenario1-kabir"),
        ("٤ حبات", "scenario1-qty4"),
    ]

    print(f"{BOLD}Scenario A: طلح أسود → كبير → ٤ حبات{RESET}")
    for msg, label in scenarios:
        reply, brain_state, order_prep, synced = _simulate_turn(
            db,
            tenant_id=tenant_id,
            conversation=conv,
            customer=customer,
            message=msg,
            brain_state=brain_state,
            order_prep=order_prep,
            recorder=recorder,
        )
        print(f"\n  Turn: {msg!r} [{label}]")
        print(f"    draft_synced={synced}")
        print(f"    outbound_reply={reply[:160]!r}{'…' if len(reply)>160 else ''}")
        items = list(order_prep.get("line_items") or brain_state.get("cart_items") or [])
        _print_line_items(items)

        if not reply.strip():
            print(f"    {RED}FAIL: silent turn — no outbound reply{RESET}")
            failures += 1
        else:
            print(f"    {GREEN}OK: outbound reply present{RESET}")

    # Validate line item evidence
    items = list(order_prep.get("line_items") or brain_state.get("cart_items") or [])
    for item in items:
        status = str(item.get("match_status") or "")
        pid = item.get("product_id")
        name = str(item.get("product_name") or "")
        if status == "confirmed" and not pid:
            print(f"    {RED}FAIL: confirmed item without product_id: {name!r}{RESET}")
            failures += 1
        if "رجال" in name and status == "confirmed":
            print(f"    {RED}FAIL: unsafe free-text confirmed: {name!r}{RESET}")
            failures += 1

    # Scenario B
    print(f"\n{BOLD}Scenario B: سمر → 10 كيلo سطل؟{RESET}")
    brain_state_b: Dict[str, Any] = {"stage": "ordering", "cart_items": []}
    order_prep_b: Dict[str, Any] = {}
    for msg in ("سمر", "10 كيلo سطل؟"):
        reply, brain_state_b, order_prep_b, synced = _simulate_turn(
            db,
            tenant_id=tenant_id,
            conversation=conv,
            customer=customer,
            message=msg,
            brain_state=brain_state_b,
            order_prep=order_prep_b,
            recorder=recorder,
        )
        print(f"\n  Turn: {msg!r}")
        print(f"    draft_synced={synced}")
        print(f"    outbound_reply={reply[:160]!r}{'…' if len(reply)>160 else ''}")
        _print_line_items(list(order_prep_b.get("line_items") or []))
        if not reply.strip():
            print(f"    {RED}FAIL: silent turn{RESET}")
            failures += 1
        else:
            print(f"    {GREEN}OK: outbound reply present{RESET}")

    items_b = list(order_prep_b.get("line_items") or [])
    if items_b:
        item = items_b[0]
        if item.get("match_status") == "confirmed" and not item.get("product_id"):
            print(f"    {RED}FAIL: confirmed without catalog evidence{RESET}")
            failures += 1
        if "10" in (reply or "") or "سطل" in (reply or "") or "كيلo" in (reply or "") or "كيلو" in (reply or ""):
            print(f"    {GREEN}OK: bucket/size guidance in reply{RESET}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", type=int, required=True, help="Tenant id to verify")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--audit-hours", type=int, default=72)
    args = parser.parse_args()

    db, _engine = _db_session()
    failures = 0

    try:
        from sqlalchemy import text  # noqa: PLC0415

        prod_count = db.execute(
            text("SELECT count(*) FROM products WHERE tenant_id=:t"),
            {"t": args.tenant},
        ).scalar()
        print(f"tenant={args.tenant} catalog_products={prod_count}")
        if prod_count == 0:
            print(f"{YELLOW}WARN: tenant has no catalog products — catalog match will be needs_review{RESET}")

        if not args.audit_only:
            recorder = _OutboundRecorder()
            failures += run_scenario(db, args.tenant, recorder)
            print(f"\n{BOLD}== Simulated message_events (outbound contract) =={RESET}")
            print(f"  outbound_events={len(recorder.events)}")
            for ev in recorder.events:
                print(
                    f"  conv={ev['conversation_id']} trigger={ev['trigger']!r} "
                    f"body={ev['body'][:100]!r}{'…' if len(ev['body'])>100 else ''}"
                )
            if len(recorder.events) < 5:
                print(f"  {RED}FAIL: expected 5 outbound events (3 + 2 turns){RESET}")
                failures += 1

        print(f"\n{BOLD}== Audit: recent draft orders vs outbound message_events =={RESET}")
        draft_count, silent_count, details = _audit_recent_drafts(
            db, args.tenant, hours=args.audit_hours,
        )
        print(f"  drafts_checked={draft_count} silent_drafts={silent_count}")
        for line in details[:10]:
            print(f"  {RED}{line}{RESET}")
        if silent_count:
            failures += silent_count

    finally:
        db.close()

    if failures:
        print(f"\n{RED}{BOLD}P0 LIVE VERIFY: FAILED ({failures} issue(s)){RESET}")
        return 1
    print(f"\n{GREEN}{BOLD}P0 LIVE VERIFY: PASSED{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
