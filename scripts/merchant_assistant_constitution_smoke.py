#!/usr/bin/env python3
"""Controlled constitution smoke — allowlisted test phones only.

Reads tenant AI settings, optionally invokes the internal webhook handler.
This is a ``direct_code_probe`` and MUST NOT be used as actual-channel E2E
evidence.

Phone values come from secret env references; none are committed or printed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [
    os.path.join(REPO, "backend"),
    os.path.join(REPO, "database"),
    REPO,
]

ALLOWED_PHONES_ENV = "NAHLA_CONSTITUTION_SMOKE_ALLOWED_PHONES"
PHONE_ENV = "NAHLA_CONSTITUTION_SMOKE_PHONE"
HASH_KEY_ENV = "NAHLA_CONSTITUTION_SMOKE_HASH_KEY"

ALLOWED_TENANTS = frozenset({31, 33})


def _allowed_phones() -> frozenset[str]:
    return frozenset(
        "".join(ch for ch in token if ch.isdigit())
        for token in (os.environ.get(ALLOWED_PHONES_ENV) or "").split(",")
        if "".join(ch for ch in token if ch.isdigit())
    )


def _phone_hash(phone: str) -> str:
    key = (os.environ.get(HASH_KEY_ENV) or "").strip()
    if not key:
        raise SystemExit(f"REFUSE: set {HASH_KEY_ENV}")
    digest = hmac.new(key.encode("utf-8"), phone.encode("utf-8"), hashlib.sha256).hexdigest()
    return "hmac-sha256:" + digest[:24]

SMOKE_MATRIX: Dict[str, List[str]] = {
    "social": [
        "السلام عليكم",
        "كيف الحال",
        "انت وش اخبارك؟",
        "شكراً",
        "الله يعطيك العافية",
    ],
    "payment": [
        "أرسل باركود الراجحي",
        "باركود الأهلي",
    ],
    "catalog": [
        "وش عندكم منتجات؟",
        "أرسل المنتجات",
    ],
    "kb": [
        "وين موقعكم؟",
        "كيف الشحن؟",
        "كيف أحفظ العسل؟",
    ],
}


def _load_settings_snapshot(db, tenant_id: int) -> Dict[str, Any]:
    from core.tenant import get_or_create_settings, merge_ai_defaults  # noqa: PLC0415
    from core.tenant import resolve_store_ai_mode  # noqa: PLC0415

    ai = merge_ai_defaults(dict(get_or_create_settings(db, tenant_id).ai_settings or {}))
    return {
        "store_ai_mode": resolve_store_ai_mode(ai),
        "allowlist": list(ai.get("ai_test_allowed_numbers") or []),
    }


def _assert_safe_to_run(settings: Dict[str, Any], phone: str) -> None:
    mode = str(settings.get("store_ai_mode") or "")
    if mode != "test":
        raise SystemExit(f"REFUSE: store_ai_mode={mode!r} (need test)")
    allow = {str(p).strip() for p in (settings.get("allowlist") or []) if p}
    if phone not in allow:
        raise SystemExit("REFUSE: test phone not in tenant allowlist")
    if phone not in _allowed_phones():
        raise SystemExit(f"REFUSE: test phone not in {ALLOWED_PHONES_ENV}")


async def _send_one(
    db,
    *,
    tenant_id: int,
    phone: str,
    text: str,
    conv_id: int,
) -> Dict[str, Any]:
    from sqlalchemy import text as sql_text  # noqa: PLC0415
    from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

    since = db.execute(
        sql_text(
            "SELECT COALESCE(MAX(id),0) FROM message_events "
            "WHERE tenant_id=:t AND conversation_id=:c"
        ),
        {"t": tenant_id, "c": conv_id},
    ).scalar() or 0

    row = db.execute(
        sql_text(
            "SELECT phone_number_id FROM whatsapp_connections "
            "WHERE tenant_id=:t ORDER BY id DESC LIMIT 1"
        ),
        {"t": tenant_id},
    ).mappings().first()
    phone_id = str(row["phone_number_id"])

    await _handle_merchant_message(
        phone_id=phone_id,
        to=phone,
        text=text,
        tenant_id=tenant_id,
        db=db,
        inbound_metadata={"normalized_type": "text", "type": "text"},
        wa_msg_id=f"wamid.constitution-smoke.{uuid.uuid4().hex[:12]}",
        wa_message_ts=datetime.now(timezone.utc),
    )
    db.commit()
    db.expire_all()

    events = db.execute(
        sql_text(
            """
            SELECT id, direction, body, metadata, created_at
            FROM message_events
            WHERE tenant_id=:t AND conversation_id=:c AND id > :since
            ORDER BY id ASC
            """
        ),
        {"t": tenant_id, "c": conv_id, "since": since},
    ).mappings().all()

    out_ev = []
    for r in events:
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        out_ev.append(
            {
                "id": r["id"],
                "direction": r["direction"],
                "body": (r.get("body") or "")[:500],
                "provider_send": meta.get("provider_send"),
                "wa_message_id": meta.get("wa_message_id"),
            }
        )

    return {
        "tenant_id": tenant_id,
        "phone_hash": _phone_hash(phone),
        "evidence_channel": "direct_code_probe",
        "conversation_id": conv_id,
        "inbound": text,
        "events": out_ev,
        "outbound_nonempty": any(
            e["direction"] == "outbound" and (e.get("body") or "").strip()
            for e in out_ev
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", type=int, required=True, help="Target tenant id")
    parser.add_argument("--conv", type=int, default=56)
    parser.add_argument("--category", choices=list(SMOKE_MATRIX.keys()), default="social")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.tenant not in ALLOWED_TENANTS:
        raise SystemExit(f"REFUSE: tenant {args.tenant} not in ALLOWED_TENANTS")
    phone = "".join(ch for ch in os.environ.get(PHONE_ENV, "") if ch.isdigit())
    if not phone or phone not in _allowed_phones():
        raise SystemExit(
            f"REFUSE: set {PHONE_ENV} and include it in {ALLOWED_PHONES_ENV}"
        )
    if not (os.environ.get(HASH_KEY_ENV) or "").strip():
        raise SystemExit(f"REFUSE: set {HASH_KEY_ENV}")

    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    report: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "settings_before": None,
        "turns": [],
    }
    try:
        report["settings_before"] = _load_settings_snapshot(db, args.tenant)
        _assert_safe_to_run(report["settings_before"], phone)

        messages = SMOKE_MATRIX[args.category]
        if args.dry_run:
            report["would_send"] = messages
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        for msg in messages:
            turn = asyncio.run(
                _send_one(
                    db,
                    tenant_id=args.tenant,
                    phone=phone,
                    text=msg,
                    conv_id=args.conv,
                )
            )
            report["turns"].append(turn)

        report["settings_after"] = _load_settings_snapshot(db, args.tenant)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
