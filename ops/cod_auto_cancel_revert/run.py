"""Restore the local status of orders the COD sweep cancelled without cause.

Owner decision (1 Oct 2026, option A): every order the COD confirmation sweep
auto-cancelled before the sweep fix (#1187) gets the status its own store shows
now, locally. The store was never changed by the sweep; only Nahla's local row
was. This script:

* selects exactly the orders the consistency check of 1 Oct listed (aliases
  t1_a1 … t1_a22: tenant + running index by order id over the sweep's
  ``order.cod.auto_cancelled`` events), and binds each alias to the creation and
  first-cancel times that check recorded — an alias that no longer points at
  the same order is skipped;
* skips any order whose state changed since that check: local status, the
  sweep's cancel history, the recorded COD decisions, the store status, or the
  store's own status history — and any order already restored;
* reads the store's status now (GET only, the integration's current token; no
  refresh, so no token is issued or stored);
* plans, per order: local status -> the store's status; metadata gains
  ``cod_auto_cancel_reverted_at`` / ``_reason`` / ``_store_status`` (the cancel
  history and ``cod_auto_cancelled_at`` stay, so the sweep's once-only rule
  still holds); one ``order.cod.auto_cancel_reverted`` system event;
* checks the deployed sweep would not act on the restored order.

Modes (NAHLA_HISTORY_TRACE_CONFIRM):

* ``COD_REVERT_DRY_RUN_V1`` — read-only session; prints the masked plan and its
  digest; writes nothing.
* ``COD_REVERT_APPLY_V1`` — refused unless NAHLA_COD_REVERT_APPROVED_DIGEST
  equals the digest of the plan computed now (so only the exact plan the owner
  approved can run); then applies it in one transaction and rolls back if any
  row does not update exactly once.

Never: a customer message, a store write, an automation event. Output carries
aliases only (no order id, number, external id, name, phone or total).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

DRY_RUN = "COD_REVERT_DRY_RUN_V1"
APPLY = "COD_REVERT_APPLY_V1"
SALLA_API_BASE = "https://api.salla.dev/admin/v2"
PRODUCTION_DB = ("postgres-ancu.railway.internal", 5432, "railway")
DIGITS = re.compile(r"\d{4,}")
REVERT_REASON = "cod_sweep_cancel_without_customer_decision"
REVERT_EVENT = "order.cod.auto_cancel_reverted"
DECISION_KEYS = ("cod_confirmed_at", "cod_pushed_external_id", "cod_confirm_requested_at",
                 "cod_cancelled_at", "cod_confirmation_bypassed")
STORE_INACTIVE = {"canceled", "cancelled", "refunded", "restored", "restoring", "returned"}

# What the consistency check of 1 Oct 08:17 UTC recorded (store_status part):
# alias -> (created, first sweep cancel, last sweep cancel, sweep cancels,
#           store status-history entries, customer confirmed).
CHECKED = {
    "t1_a1": ("2026-04-14 08:39:09", "2026-05-18 17:14:54.210207", "2026-05-18 17:14:54.210207", 1, 2, False),
    "t1_a2": ("2026-04-10 14:54:19", "2026-05-18 17:14:54.210030", "2026-05-18 17:14:54.210030", 1, 1, False),
    "t1_a3": ("2026-04-23 13:58:19", "2026-05-18 17:14:54.210314", "2026-05-18 17:14:54.210314", 1, 2, False),
    "t1_a4": ("2026-07-02 02:47:58", "2026-07-03 02:49:15.962544", "2026-07-03 16:46:12.803248", 8, 1, False),
    "t1_a5": ("2026-07-02 14:20:25", "2026-07-03 14:21:23.884030", "2026-07-03 16:46:12.803512", 8, 1, False),
    "t1_a6": ("2026-07-31 10:27:30", "2026-08-01 10:31:56.046655", "2026-08-03 00:01:49.705564", 135, 1, False),
    "t1_a7": ("2026-09-12 16:08:25", "2026-09-13 16:08:36.572007", "2026-09-14 00:00:39.921173", 96, 2, False),
    "t1_a8": ("2026-09-12 17:51:23", "2026-09-13 17:53:29.006060", "2026-09-14 00:00:39.951918", 74, 1, False),
    "t1_a9": ("2026-09-12 18:10:17", "2026-09-13 18:13:32.774041", "2026-09-14 00:00:39.921381", 70, 1, False),
    "t1_a10": ("2026-09-12 19:00:03", "2026-09-13 19:04:01.258717", "2026-09-14 00:00:39.940561", 60, 2, False),
    "t1_a11": ("2026-09-12 19:31:38", "2026-09-13 19:34:06.956907", "2026-09-14 00:00:39.921499", 54, 2, False),
    "t1_a12": ("2026-09-13 07:55:48", "2026-09-14 07:57:36.730006", "2026-09-14 23:59:37.444393", 195, 1, False),
    "t1_a13": ("2026-09-13 08:00:25", "2026-09-14 08:02:37.478065", "2026-09-14 23:59:37.451315", 194, 1, False),
    "t1_a14": ("2026-09-13 08:04:53", "2026-09-14 08:07:38.283209", "2026-09-14 23:59:37.451391", 193, 1, False),
    "t1_a15": ("2026-09-13 09:22:33", "2026-09-14 09:27:11.998661", "2026-09-14 23:59:37.451093", 177, 1, False),
    "t1_a16": ("2026-09-14 18:29:11", "2026-09-15 18:30:17.079093", "2026-09-16 00:04:08.509616", 70, 3, True),
    "t1_a17": ("2026-09-15 09:20:11", "2026-09-16 09:20:25.533016", "2026-09-17 00:03:32.455586", 177, 4, True),
    "t1_a18": ("2026-09-26 16:12:01", "2026-09-27 16:12:11.997657", "2026-09-28 00:00:14.490111", 93, 1, False),
    "t1_a19": ("2026-09-26 17:23:02", "2026-09-27 17:27:27.919486", "2026-09-28 00:00:14.489811", 78, 1, False),
    "t1_a20": ("2026-09-26 18:42:21", "2026-09-27 18:43:27.178797", "2026-09-28 00:00:14.489927", 64, 1, False),
    "t1_a21": ("2026-09-26 18:45:03", "2026-09-27 18:48:28.123718", "2026-09-28 00:00:14.489489", 63, 3, True),
    "t1_a22": ("2026-09-27 08:14:34", "2026-09-28 08:17:22.310709", "2026-09-28 23:57:30.331375", 187, 4, True),
}
CHECKED_LOCAL_STATUS = "cancelled"
CHECKED_STORE_STATUS = "under_review"


def emit(kind, value):
    print("COD_REVERT=" + json.dumps({"part": kind, **value}, ensure_ascii=False, default=str), flush=True)


def mask(value, limit=40):
    return DIGITS.sub("[digits]", str(value or ""))[:limit]


def ts(value):
    if isinstance(value, dict):
        value = value.get("date")
    if isinstance(value, datetime):
        out = value
    elif value:
        try:
            out = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    if out.tzinfo is not None:
        out = out.astimezone(timezone.utc).replace(tzinfo=None)
    return out


def same_time(a, b):
    a, b = ts(a), ts(b)
    return a is not None and b is not None and abs((a - b).total_seconds()) < 0.001


# ── store reads (GET only, no token refresh) ────────────────────────────────

def store_token(conn, tenant):
    rows = conn.execute(text(
        "SELECT id, enabled, config FROM integrations WHERE tenant_id = :t AND provider = 'salla' "
        "ORDER BY id"), {"t": tenant}).all()
    enabled = [(i, c or {}) for i, e, c in rows if e]
    if len(enabled) > 1:
        enabled = [(i, c) for i, c in enabled if c.get("is_canonical")]
    if len(enabled) != 1:
        return None, "integration_ambiguous_or_missing"
    cfg = enabled[0][1]
    if not cfg.get("api_key") or cfg.get("needs_reauth"):
        return None, "no_usable_token"
    expires = ts(cfg.get("expires_at") or cfg.get("token_expires_at"))
    if expires is not None and expires <= datetime.now(timezone.utc).replace(tzinfo=None):
        return None, "token_expired_not_refreshed"
    return str(cfg["api_key"]), "ok"


def store_get(client, token, path):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = client.get(SALLA_API_BASE + path, headers=headers)
    if resp.status_code == 429:
        time.sleep(5)
        resp = client.get(SALLA_API_BASE + path, headers=headers)
    return resp


def read_store(client, token, external_id):
    """(state, store status slug, store status-history entries)."""
    resp = store_get(client, token, f"/orders/{external_id}")
    if resp.status_code in (401, 403):
        return "token_rejected", None, None
    if resp.status_code == 404:
        return "not_found_in_store", None, None
    if resp.status_code != 200:
        return f"store_http_{resp.status_code}", None, None
    raw = (resp.json() or {}).get("data") or {}
    status = raw.get("status")
    slug = str((status.get("slug") if isinstance(status, dict) else status) or "").strip().lower()
    time.sleep(0.4)
    hist = store_get(client, token, f"/orders/{external_id}/histories")
    entries = None
    if hist.status_code == 200:
        data = (hist.json() or {}).get("data")
        entries = len([e for e in data if isinstance(e, dict)]) if isinstance(data, list) else None
    return "read", slug, entries


# ── the plan ─────────────────────────────────────────────────────────────────

def sweep_cancels(conn):
    return conn.execute(text(
        "SELECT o.id, o.tenant_id, o.external_id, o.status, o.source, o.metadata, "
        "e.cnt, e.first_at, e.last_at FROM orders o JOIN ("
        "  SELECT tenant_id, reference_id, count(*) AS cnt, min(created_at) AS first_at, "
        "  max(created_at) AS last_at FROM system_events "
        "  WHERE event_type = 'order.cod.auto_cancelled' GROUP BY tenant_id, reference_id) e "
        "ON e.tenant_id = o.tenant_id AND e.reference_id = o.id::text "
        "ORDER BY o.tenant_id, o.id")).all()


def decision_events(conn, tenant, order_id):
    return {event_type for (event_type,) in conn.execute(text(
        "SELECT DISTINCT event_type FROM system_events WHERE tenant_id = :t AND reference_id = :r "
        "AND event_type IN ('order.cod.confirmed', 'order.cod.cancelled', :rev)"),
        {"t": tenant, "r": str(order_id), "rev": REVERT_EVENT}).all()}


def sweep_would_act(order_id, status, meta, decided_events):
    """The deployed sweep's own predicate on the restored row."""
    from services.cod_confirmation import cod_awaits_customer_decision  # noqa: PLC0415

    row = SimpleNamespace(id=order_id, status=status, extra_metadata=meta)
    refs = {str(order_id)} if decided_events & {"order.cod.confirmed", "order.cod.cancelled"} else set()
    return bool(cod_awaits_customer_decision(row, decided_refs=refs) and not meta.get("cod_auto_cancelled_at"))


def build_plan(conn, client):
    rows = sweep_cancels(conn)
    index, tokens, plan, found = {}, {}, [], set()
    for order_id, tenant, external_id, status, source, meta, count, first_at, last_at in rows:
        meta = meta if isinstance(meta, dict) else {}
        index[tenant] = index.get(tenant, 0) + 1
        alias = f"t{tenant}_a{index[tenant]}"
        if alias not in CHECKED:
            emit("not_in_checked_list", {"alias": alias})
            continue
        found.add(alias)
        created, first, last, cancels, history, confirmed = CHECKED[alias]
        local = str(status or "").strip().lower()
        events = decision_events(conn, tenant, order_id)
        is_confirmed = any(meta.get(k) for k in ("cod_confirmed_at", "cod_pushed_external_id",
                                                 "cod_confirm_requested_at")) or "order.cod.confirmed" in events
        row = {"alias": alias, "local_status": mask(local)}
        skip, store_status = None, None
        if not (same_time(meta.get("created_at"), created) and same_time(first_at, first)):
            skip = "alias_points_at_another_order"
        elif meta.get("cod_auto_cancel_reverted_at") or REVERT_EVENT in events:
            skip = "already_restored"
        elif local != CHECKED_LOCAL_STATUS:
            skip = "local_status_changed_since_check"
        elif int(count) != cancels or not same_time(last_at, last):
            skip = "sweep_history_changed_since_check"
        elif is_confirmed != confirmed or meta.get("cod_cancelled_at") or "order.cod.cancelled" in events:
            skip = "cod_decision_changed_since_check"
        elif str(source or "").strip().lower() != "salla" or not external_id:
            skip = "not_a_store_order"
        if skip is None:
            if tenant not in tokens:
                tokens[tenant] = store_token(conn, tenant)
            token, token_state = tokens[tenant]
            if token is None:
                skip = token_state
            else:
                try:
                    state, store_status, entries = read_store(client, token, external_id)
                except Exception as exc:  # noqa: BLE001 - this order is skipped, the others still read
                    state, store_status, entries = f"read_failed:{type(exc).__name__}", None, None
                row.update({"store_status": mask(store_status), "store_history_entries": entries})
                if state == "token_rejected":
                    tokens[tenant] = (None, "token_rejected")
                if state != "read":
                    skip = state
                elif store_status != CHECKED_STORE_STATUS or entries != history:
                    skip = "store_changed_since_check"
                elif store_status in STORE_INACTIVE:
                    skip = "store_not_active"
                time.sleep(0.4)
        if skip is None:
            after_meta = {**meta, "cod_auto_cancel_reverted_at": "<apply time>"}
            if sweep_would_act(order_id, store_status, after_meta, events):
                skip = "sweep_would_act_after_restore"
        if skip is not None:
            row["decision"] = "skip:" + skip
        else:
            row.update({"decision": "restore", "status_after": store_status,
                        "customer_confirmed": confirmed})
        plan.append((row, order_id, tenant, store_status))
        emit("plan_row", row)
    for alias in sorted(set(CHECKED) - found, key=lambda a: int(a.split("_a")[1])):
        row = {"alias": alias, "decision": "skip:not_found_now"}
        plan.append((row, None, None, None))
        emit("plan_row", row)
    restore = [p for p in plan if p[0]["decision"] == "restore"]
    digest = hashlib.sha256(json.dumps(
        [(p[0]["alias"], p[1], p[2], p[3]) for p in restore], sort_keys=True).encode()).hexdigest()[:16]
    summary = {}
    for p in plan:
        summary[p[0]["decision"]] = summary.get(p[0]["decision"], 0) + 1
    emit("plan_summary", {"by_decision": summary, "restore": len(restore), "plan_digest": digest})
    return restore, digest


def apply_plan(conn, restore):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row, order_id, tenant, store_status in restore:
        patch = {"cod_auto_cancel_reverted_at": now.isoformat(),
                 "cod_auto_cancel_reverted_reason": REVERT_REASON,
                 "cod_auto_cancel_reverted_store_status": store_status}
        updated = conn.execute(text(
            "UPDATE orders SET status = :s, metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:p AS jsonb) "
            "WHERE id = :i AND tenant_id = :t AND lower(status) = :was"),
            {"s": store_status, "p": json.dumps(patch), "i": order_id, "t": tenant,
             "was": CHECKED_LOCAL_STATUS}).rowcount
        if updated != 1:
            raise RuntimeError(f"row_not_updated_once:{row['alias']}")
        conn.execute(text(
            "INSERT INTO system_events (tenant_id, category, event_type, severity, summary, payload, "
            "reference_id, created_at) VALUES (:t, 'order', :e, 'info', :sm, CAST(:p AS jsonb), :r, :c)"),
            {"t": tenant, "e": REVERT_EVENT, "sm": "Local status restored to the store's status",
             "p": json.dumps({"reason": REVERT_REASON, "reverted_from": CHECKED_LOCAL_STATUS,
                              "store_status": store_status, "source": "owner_approved_correction"}),
             "r": str(order_id), "c": now})
        emit("applied", {"alias": row["alias"], "status_after": store_status})


def main():
    mode = os.environ.get("NAHLA_HISTORY_TRACE_CONFIRM")
    if mode not in (DRY_RUN, APPLY):
        emit("status", {"status": "idle"})
        return 0
    try:
        import httpx  # noqa: PLC0415

        raw = os.environ.get("DATABASE_URL", "")
        parsed = make_url(raw.replace("postgres://", "postgresql://", 1))
        if (parsed.host, parsed.port or 5432, parsed.database) != PRODUCTION_DB:
            emit("status", {"status": "target_refused"})
            return 1
        options = "-c statement_timeout=15000"
        if mode == DRY_RUN:
            options += " -c default_transaction_read_only=on"
        engine = create_engine(parsed, connect_args={"options": options})
        with engine.connect() as conn, httpx.Client(timeout=20.0, follow_redirects=False) as client:
            read_only = conn.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
            if mode == DRY_RUN and not read_only:
                emit("status", {"status": "not_read_only"})
                return 1
            emit("status", {"status": "planning", "mode": "dry_run" if mode == DRY_RUN else "apply",
                            "read_only_session": read_only})
            restore, digest = build_plan(conn, client)
            if mode == DRY_RUN:
                conn.rollback()
                emit("status", {"status": "done", "mode": "dry_run", "written": 0})
                return 0
            approved = (os.environ.get("NAHLA_COD_REVERT_APPROVED_DIGEST") or "").strip()
            if not approved or approved != digest:
                conn.rollback()
                emit("status", {"status": "apply_refused", "reason": "digest_mismatch", "plan_digest": digest})
                return 1
            try:
                apply_plan(conn, restore)
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - nothing is kept unless every row applied
                conn.rollback()
                emit("status", {"status": "apply_rolled_back", "error_type": type(exc).__name__,
                                "detail": mask(str(exc), 80)})
                return 1
            emit("status", {"status": "done", "mode": "apply", "written": len(restore)})
        return 0
    except Exception as exc:  # raw errors may carry query values; only the type is logged
        emit("status", {"status": "operator_failed", "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
