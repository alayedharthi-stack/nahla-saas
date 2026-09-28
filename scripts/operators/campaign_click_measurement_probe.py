#!/usr/bin/env python3
"""Read-only probe: can button clicks be measured for one campaign, and how?

Answers, for one tenant / campaign, with evidence and without changing anything:

* the campaign's template and its buttons (URL static / URL dynamic suffix /
  QUICK_REPLY / COPY_CODE / PHONE_NUMBER) from our synced copy and, when Meta is
  reachable, from Meta's own copy (``message_templates`` incl.
  ``cta_url_link_tracking_opted_out``);
* whether Template Analytics is enabled on the WABA (``is_enabled_for_insights``)
  and what the token we hold can do (``debug_token`` scopes, token source);
* what Meta's ``template_analytics`` returns for this template over the
  campaign's send window (it is per template per day, never per campaign);
* whether the same template was sent by other campaigns of the tenant on the
  same days (attribution of a template-level count to this campaign);
* our own ledger view of the campaign (message / recipient scopes separate).

Read-only by construction: one ``REPEATABLE READ, READ ONLY`` transaction
(``PGOPTIONS=-c default_transaction_read_only=on`` recommended as a second
belt), Graph API GET requests only, the token context is built without the
persisting resolver, and nothing is POSTed. It never enables insights, never
edits a template, never sends a message. Output is JSON: no phone numbers, no
token, customer data absent; template URLs are reduced to their host.

    python scripts/operators/campaign_click_measurement_probe.py \\
        --tenant-id 33 --campaign-id 35 [--skip-graph]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "backend", ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _iso(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        return v.replace(microsecond=0).isoformat()
    return None


def _host_only(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/…" if parts.netloc else "…"
    except Exception:  # noqa: BLE001
        return "…"


def _classify_buttons(components: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(components, list):
        return out
    for comp in components:
        if not isinstance(comp, dict) or str(comp.get("type", "")).upper() != "BUTTONS":
            continue
        for idx, btn in enumerate(comp.get("buttons") or []):
            if not isinstance(btn, dict):
                continue
            btype = str(btn.get("type", "")).upper()
            entry: Dict[str, Any] = {"index": idx, "type": btype, "text_len": len(str(btn.get("text") or ""))}
            if btype == "URL":
                url = str(btn.get("url") or "")
                entry["url_host"] = _host_only(url)
                entry["dynamic_suffix"] = "{{" in url
                entry["measurable_by"] = (
                    ["meta_template_analytics(url_button, daily, per template)", "tracked_redirect_link(per message)"]
                    if entry["dynamic_suffix"]
                    else ["meta_template_analytics(url_button, daily, per template)",
                          "tracked_redirect_link requires a dynamic suffix → new template version"]
                )
            elif btype == "QUICK_REPLY":
                entry["measurable_by"] = ["inbound context.id (per message, live)",
                                          "meta_template_analytics(quick_reply_button, daily)"]
            else:
                entry["measurable_by"] = ["none"]
            out.append(entry)
    return out


def _columns(db: Session, table: str) -> set:
    rows = db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0] for r in rows}


def _db_section(db: Session, tenant_id: int, campaign_id: int) -> Dict[str, Any]:
    """Raw SQL only: the image may carry newer models than the database (the
    boot-time ``safe_alters`` of the new backend add columns), so every new
    column is read only when ``information_schema`` says it exists."""
    from database.models import WhatsAppTemplate  # noqa: PLC0415

    res: Dict[str, Any] = {}
    camp_cols = _columns(db, "campaigns")
    att_cols = _columns(db, "campaign_send_attempts")
    log_cols = _columns(db, "campaign_send_logs")
    res["schema"] = {
        "campaigns.offer_expires_at": "offer_expires_at" in camp_cols,
        "campaigns.content_revision": "content_revision" in camp_cols,
        "campaign_send_attempts.click_trackable": "click_trackable" in att_cols,
        "campaign_send_attempts.clicked_at": "clicked_at" in att_cols,
        "campaign_send_logs.clicked_at": "clicked_at" in log_cols,
        "campaign_content_revisions": bool(_columns(db, "campaign_content_revisions")),
    }
    opt = [c for c in ("offer_expires_at", "content_revision", "launched_at") if c in camp_cols]
    cols = "id, tenant_id, status, template_id, template_name, template_language, created_at, audience_count"
    if opt:
        cols += ", " + ", ".join(opt)
    row = db.execute(text(f"SELECT {cols} FROM campaigns WHERE id = :cid"), {"cid": campaign_id}).mappings().first()
    if row is None or int(row["tenant_id"]) != int(tenant_id):
        return {"error": "campaign_not_found_for_tenant"}
    camp = dict(row)
    res["campaign"] = {
        "id": camp["id"], "status": camp["status"], "template_id": camp["template_id"],
        "template_name": camp["template_name"], "template_language": camp["template_language"],
        "content_revision": camp.get("content_revision"), "offer_expires_at": _iso(camp.get("offer_expires_at")),
        "created_at": _iso(camp["created_at"]), "launched_at": _iso(camp.get("launched_at")),
        "audience_count": camp["audience_count"],
    }

    # Our synced copy of the template (this table has no new columns).
    tpl = None
    q = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id)
    tid = camp["template_id"]
    if tid and str(tid).isdigit():
        tpl = q.filter(WhatsAppTemplate.id == int(tid)).first()
    if tpl is None and tid:
        tpl = q.filter(WhatsAppTemplate.meta_template_id == str(tid)).first()
    if tpl is None and camp["template_name"]:
        tpl = q.filter(WhatsAppTemplate.name == camp["template_name"]).order_by(WhatsAppTemplate.id.desc()).first()
    if tpl is not None:
        res["template_local"] = {
            "id": tpl.id, "meta_template_id": tpl.meta_template_id, "name": tpl.name, "language": tpl.language,
            "category": tpl.category, "status": tpl.status, "synced_at": _iso(tpl.synced_at),
            "buttons": _classify_buttons(tpl.components),
        }
        try:
            from services.campaign_send_ledger import template_click_trackable  # noqa: PLC0415
            res["template_local"]["quick_reply_trackable_now"] = bool(template_click_trackable(tpl))
        except Exception as exc:  # noqa: BLE001
            res["template_local"]["quick_reply_trackable_now"] = f"error:{type(exc).__name__}"
    else:
        res["template_local"] = None

    # Ledger view, message scope (attempts) and recipient scope (send logs) kept apart.
    click_att = "click_trackable" in att_cols and "clicked_at" in att_cols
    extra = (", COUNT(*) FILTER (WHERE click_trackable IS TRUE) AS click_trackable_messages"
             ", COUNT(*) FILTER (WHERE clicked_at IS NOT NULL) AS clicked_messages") if click_att else ""
    att = db.execute(text(
        "SELECT COUNT(*) AS attempts, COUNT(*) FILTER (WHERE accepted_at IS NOT NULL) AS accepted, "
        "COUNT(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered, "
        "COUNT(*) FILTER (WHERE read_at IS NOT NULL) AS read, "
        "COUNT(*) FILTER (WHERE failed_at IS NOT NULL) AS failed, "
        "COUNT(*) FILTER (WHERE accepted_at IS NOT NULL AND failed_at IS NOT NULL) AS failed_after_accept, "
        "MIN(accepted_at) AS first_accepted_at, MAX(accepted_at) AS last_accepted_at, "
        "GREATEST(MAX(accepted_at), MAX(delivered_at), MAX(read_at), MAX(failed_at)) AS last_event_at"
        f"{extra} FROM campaign_send_attempts WHERE campaign_id = :cid AND tenant_id = :tid"
    ), {"cid": campaign_id, "tid": tenant_id}).mappings().one()
    res["attempts_message_scope"] = {k: (_iso(v) if isinstance(v, datetime) else (int(v) if v is not None else None))
                                     for k, v in dict(att).items()}
    if not click_att:
        res["attempts_message_scope"]["click_trackable_messages"] = "column_missing_until_new_backend_boots"
        res["attempts_message_scope"]["clicked_messages"] = "column_missing_until_new_backend_boots"
    states = db.execute(text(
        "SELECT state, COUNT(*) FROM campaign_send_attempts WHERE campaign_id = :cid GROUP BY 1 ORDER BY 1"
    ), {"cid": campaign_id}).all()
    res["attempt_states"] = {str(s): int(n) for s, n in states}
    err = db.execute(text(
        "SELECT COALESCE(error_code, '') , COUNT(*) FROM campaign_send_attempts "
        "WHERE campaign_id = :cid AND failed_at IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
    ), {"cid": campaign_id}).all() if "error_code" in att_cols else []
    res["attempt_failure_codes"] = {str(c) or "(none)": int(n) for c, n in err}

    logs = db.execute(text(
        "SELECT status, COUNT(*) FROM campaign_send_logs WHERE campaign_id = :cid GROUP BY 1 ORDER BY 1"
    ), {"cid": campaign_id}).all()
    res["send_log_recipient_scope"] = {"by_status": {str(s): int(n) for s, n in logs}}
    rec = db.execute(text(
        "SELECT COUNT(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered, "
        "COUNT(*) FILTER (WHERE read_at IS NOT NULL) AS read, "
        "COUNT(*) FILTER (WHERE failed_at IS NOT NULL) AS failed, "
        "COUNT(*) FILTER (WHERE status = 'sent') AS meta_accepted, "
        "COUNT(*) FILTER (WHERE status = 'sent' AND delivered_at IS NULL AND failed_at IS NULL) AS accepted_not_delivered, "
        "COUNT(*) FILTER (WHERE status = 'queued') AS queued, "
        "COUNT(*) FILTER (WHERE status LIKE 'skipped%') AS excluded "
        "FROM campaign_send_logs WHERE campaign_id = :cid"
    ), {"cid": campaign_id}).mappings().one()
    res["send_log_recipient_scope"].update({k: int(v or 0) for k, v in dict(rec).items()})
    if "error_code" in log_cols:
        lerr = db.execute(text(
            "SELECT COALESCE(error_code, ''), COUNT(*) FROM campaign_send_logs "
            "WHERE campaign_id = :cid AND status IN ('failed', 'sent') AND error_code IS NOT NULL "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
        ), {"cid": campaign_id}).all()
        res["send_log_recipient_scope"]["error_codes"] = {str(c): int(n) for c, n in lerr}

    # Lease / pause state as the dashboard reads it.
    if _columns(db, "campaign_dispatch_leases"):
        lease_cols = _columns(db, "campaign_dispatch_leases")
        want = [c for c in ("worker_id", "heartbeat_at", "stop_requested", "pause_reason", "pause_detail",
                            "paused_at", "lease_expires_at", "updated_at") if c in lease_cols]
        lease = db.execute(text(f"SELECT {', '.join(want)} FROM campaign_dispatch_leases WHERE campaign_id = :cid"),
                           {"cid": campaign_id}).mappings().first()
        res["lease"] = ({k: (_iso(v) if isinstance(v, datetime) else v) for k, v in dict(lease).items()
                         if k != "worker_id"} if lease else None)

    # Same template sent by other campaigns of the tenant on the same days.
    first, last = att["first_accepted_at"], att["last_accepted_at"]
    per_day = db.execute(text(
        "SELECT DATE(accepted_at), COUNT(*) FROM campaign_send_attempts "
        "WHERE campaign_id = :cid AND accepted_at IS NOT NULL GROUP BY 1 ORDER BY 1"
    ), {"cid": campaign_id}).all()
    res["accepted_per_day_utc"] = {str(d): int(n) for d, n in per_day}
    res["send_window"] = {"first_accepted_at": _iso(first), "last_accepted_at": _iso(last)}
    if first and last:
        rows = db.execute(text(
            "SELECT a.campaign_id, DATE(a.accepted_at) AS day, COUNT(*) "
            "FROM campaign_send_attempts a JOIN campaigns c ON c.id = a.campaign_id "
            "WHERE a.tenant_id = :tid AND a.campaign_id <> :cid AND a.accepted_at IS NOT NULL "
            "AND DATE(a.accepted_at) BETWEEN DATE(:d0) AND DATE(:d1) "
            "AND (c.template_id = :tpl_id OR (c.template_name IS NOT NULL AND c.template_name = :tpl_name)) "
            "GROUP BY 1, 2 ORDER BY 2, 1"
        ), {"tid": tenant_id, "cid": campaign_id, "d0": first, "d1": last,
            "tpl_id": camp["template_id"] or "", "tpl_name": camp["template_name"] or ""}).all()
        res["same_template_other_campaigns_same_days"] = [
            {"campaign_id": int(r[0]), "day": str(r[1]), "accepted": int(r[2])} for r in rows
        ]
        others = db.execute(text(
            "SELECT id, status FROM campaigns WHERE tenant_id = :tid AND id <> :cid "
            "AND (template_id = :tpl_id OR (template_name IS NOT NULL AND template_name = :tpl_name)) ORDER BY id"
        ), {"tid": tenant_id, "cid": campaign_id, "tpl_id": camp["template_id"] or "",
            "tpl_name": camp["template_name"] or ""}).all()
        res["same_template_other_campaigns_any_time"] = [{"campaign_id": int(i), "status": s} for i, s in others]
    else:
        res["same_template_other_campaigns_same_days"] = []

    today = datetime.now(timezone.utc).date()
    res["meta_7day_click_window_by_send_day"] = {
        str(d): ("open" if (today - d).days < 7 else "closed") for d, _ in per_day
    }
    return res


async def _graph_section(db: Session, tenant_id: int, template_name: Optional[str],
                         meta_template_id: Optional[str], window: Dict[str, Any]) -> Dict[str, Any]:
    from database.models import WhatsAppConnection  # noqa: PLC0415
    from services.whatsapp_platform.service import provider_get_with_context  # noqa: PLC0415
    from services.whatsapp_platform.token_manager import get_token_candidates  # noqa: PLC0415

    out: Dict[str, Any] = {}
    conn = db.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tenant_id).first()
    if conn is None:
        return {"error": "no_whatsapp_connection"}
    waba = getattr(conn, "whatsapp_business_account_id", None)
    out["connection"] = {"status": conn.status, "connection_type": getattr(conn, "connection_type", None),
                         "provider": getattr(conn, "provider", None), "waba_id_present": bool(waba),
                         "phone_number_id_present": bool(getattr(conn, "phone_number_id", None))}
    candidates = [c for c in get_token_candidates(conn) if c.token]
    out["token_candidates"] = [{"source": c.source, "status": c.token_status,
                                "expires_at": _iso(c.expires_at)} for c in candidates]
    if not candidates or not waba:
        out["error"] = "no_token_or_waba"
        return out

    async def get(path: str, params: Optional[Dict[str, Any]] = None, op: str = "click_probe") -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for ctx in candidates:
            try:
                data = await provider_get_with_context(conn, ctx, tenant_id=tenant_id, operation=op,
                                                       path=path, params=params)
            except Exception as exc:  # noqa: BLE001
                data = {"error": {"probe_exception": type(exc).__name__}}
            results[ctx.source] = data
            if isinstance(data, dict) and "error" not in data:
                break
        return results

    # 1. Token scopes.
    scopes = {}
    for ctx in candidates:
        try:
            data = await provider_get_with_context(conn, ctx, tenant_id=tenant_id, operation="click_probe_debug",
                                                   path="debug_token", params={"input_token": ctx.token})
            d = data.get("data") if isinstance(data, dict) else None
            scopes[ctx.source] = ({"scopes": d.get("scopes"), "type": d.get("type"), "is_valid": d.get("is_valid"),
                                   "expires_at": d.get("expires_at"), "granular_waba_ids": [
                                       g.get("target_ids") for g in (d.get("granular_scopes") or [])
                                       if g.get("scope") == "whatsapp_business_management"]}
                                  if isinstance(d, dict) else {"raw_error": data.get("error")})
        except Exception as exc:  # noqa: BLE001
            scopes[ctx.source] = {"probe_exception": type(exc).__name__}
    out["token_scopes"] = scopes

    # 2. WABA insights flag.
    out["waba"] = await get(str(waba), {"fields": "id,name,is_enabled_for_insights,timezone_id"})

    # 3. Meta's copy of the template.
    if template_name:
        tpls = await get(f"{waba}/message_templates", {
            "name": template_name,
            "fields": "id,name,status,category,language,components,cta_url_link_tracking_opted_out",
        })
        summary: Dict[str, Any] = {}
        for src, data in tpls.items():
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                summary[src] = [{
                    "id": t.get("id"), "name": t.get("name"), "status": t.get("status"), "category": t.get("category"),
                    "language": t.get("language"),
                    "cta_url_link_tracking_opted_out": t.get("cta_url_link_tracking_opted_out"),
                    "buttons": _classify_buttons(t.get("components")),
                } for t in data["data"] if isinstance(t, dict)]
                if not meta_template_id and summary[src]:
                    meta_template_id = summary[src][0].get("id")
            else:
                summary[src] = data
        out["meta_template"] = summary

    # 4. Template analytics over the send window (read only; errors reported as-is).
    if meta_template_id and window.get("first_accepted_at"):
        start = datetime.fromisoformat(window["first_accepted_at"]).replace(tzinfo=timezone.utc)
        end = min(datetime.now(timezone.utc), start + timedelta(days=89))
        out["template_analytics"] = await get(f"{waba}/template_analytics", {
            "start": int(start.timestamp()), "end": int(end.timestamp()), "granularity": "DAILY",
            "metric_types": "SENT,DELIVERED,READ,CLICKED", "template_ids": json.dumps([str(meta_template_id)]),
        })
    else:
        out["template_analytics"] = {"skipped": "no_meta_template_id_or_no_sends"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant-id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--skip-graph", action="store_true")
    args = ap.parse_args()
    if not args.database_url:
        print(json.dumps({"error": "DATABASE_URL missing"}))
        return 2
    engine = create_engine(args.database_url, pool_pre_ping=True)
    report: Dict[str, Any] = {"probe": "campaign_click_measurement", "generated_at": _iso(datetime.now(timezone.utc)),
                              "tenant_id": args.tenant_id, "campaign_id": args.campaign_id, "read_only": True}
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        report["db"] = _db_section(db, args.tenant_id, args.campaign_id)
        if not args.skip_graph and "error" not in report["db"]:
            tl = report["db"].get("template_local") or {}
            camp = report["db"].get("campaign") or {}
            try:
                report["graph"] = asyncio.run(_graph_section(
                    db, args.tenant_id, camp.get("template_name") or tl.get("name"),
                    tl.get("meta_template_id"), report["db"].get("send_window") or {}))
            except Exception as exc:  # noqa: BLE001
                report["graph"] = {"error": f"{type(exc).__name__}: {exc}"}
        db.rollback()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
