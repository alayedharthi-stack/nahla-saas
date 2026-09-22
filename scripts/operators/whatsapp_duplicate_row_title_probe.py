"""Does WhatsApp actually refuse a list whose two rows share a visible title?

The platform de-duplicates interactive rows by their visible title. The
recorded evidence for that rule is all about **reply buttons** — HTTP 400 with
``error_data.details = "Duplicate button title"``, ``build_standard_pick_buttons``,
``meta_errors.invalid_payload``. For **list rows** the rule was carried over by
analogy; no test and no logged rejection in this repository establishes it.

It matters: a catalogue like Tenant 1's, where five dresses are all titled
«فستان», is shaped entirely by that rule. If rows only require unique **ids**,
the constraint is self-imposed.

**Answered, 2026-09-22 10:17Z:** one list, two rows, one title, two ids —
``{"accepted": true, "classification": "ok", "http_status": 200}``, no error of
any kind. WhatsApp does not refuse a list for a repeated row title. The rule
was self-imposed, and ``_send_list_reply`` no longer drops those rows; it logs
the repeat and sends them. Labelling them so the *customer* can tell them apart
remains the caller's job (``core/commerce_runtime/choice_rows.py``).

This stays runnable so the answer can be re-established after any provider
change. It asks the provider, once. It sends a single interactive list to the
tenant's own allowlisted trial recipient with two rows that share a title and
differ only by id, and prints what came back. Owner-approved, and deliberately
narrow:

* the recipient is read from the pilot's own allowlist, never typed here, and
  only ever the first entry;
* the send goes through the platform's ``provider_send_message``, so the
  credential is resolved, refreshed and kept inside the platform's own code
  and never touches this script;
* ``NAHLA_DUPLICATE_ROW_TITLE_TEST=SEND`` is required, so a stray deploy of
  this image sends nothing;
* exactly one message, and nothing is written to any table.

Output is one ``ROW_TITLE_PROBE=`` JSON line: the HTTP status, the provider's
classification and error fields, whether a message id came back, and the
recipient masked. No token, no full number, no message body beyond the two row
titles this test is about.

Usage::

    DATABASE_URL=... NAHLA_DUPLICATE_ROW_TITLE_TEST=SEND \\
      python scripts/operators/whatsapp_duplicate_row_title_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "backend"), str(ROOT / "database")):
    if path not in sys.path:
        sys.path.insert(0, path)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

SHARED_TITLE = "فستان"
ROW_IDS = ("nahla_probe_row_a", "nahla_probe_row_b")
BODY = "اختبار فني داخلي — لا حاجة لأي إجراء."


def _mask(phone: str) -> str:
    text = str(phone or "").strip()
    return text[:5] + "*" * max(0, len(text) - 7) + text[-2:] if len(text) > 7 else "***"


def _first_allowlisted_recipient() -> str:
    raw = str(os.environ.get("COMMERCE_RUNTIME_PILOT_RECIPIENT_ALLOWLIST", "") or "")
    for part in raw.replace(";", ",").split(","):
        candidate = part.strip()
        if candidate:
            return candidate
    return ""


def _payload(to: str) -> Dict[str, Any]:
    """One list, two rows, one title between them, two ids."""
    return {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": BODY},
            "action": {
                "button": "اختر",
                "sections": [{"rows": [
                    {"id": ROW_IDS[0], "title": SHARED_TITLE, "description": "144 SAR"},
                    {"id": ROW_IDS[1], "title": SHARED_TITLE, "description": "114 SAR"},
                ]}],
            },
        },
    }


def _error_view(data: Any) -> Dict[str, Any]:
    body = data if isinstance(data, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    details = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    return {
        "accepted": bool(messages and isinstance(messages[0], dict) and messages[0].get("id")),
        "classification": body.get("_nahla_classification"),
        "http_status": body.get("_nahla_http_status") or body.get("status_code"),
        "error_code": error.get("code"),
        "error_subcode": error.get("error_subcode"),
        "error_type": error.get("type"),
        "error_message": error.get("message"),
        "error_details": details.get("details"),
    }


async def _run(session: Session, tenant_id: int, to: str) -> Dict[str, Any]:
    from models import WhatsAppConnection  # noqa: PLC0415
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    conn = (
        session.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id,
                WhatsAppConnection.status == "connected")
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )
    if conn is None or not getattr(conn, "phone_number_id", ""):
        return {"skipped": "no_connected_whatsapp_connection"}
    data, _token = await provider_send_message(
        session, conn,
        tenant_id=tenant_id,
        operation="duplicate_row_title_probe",
        phone_id=str(conn.phone_number_id),
        payload=_payload(to),
        timeout=20,
    )
    return _error_view(data)


def main() -> int:
    if str(os.environ.get("NAHLA_DUPLICATE_ROW_TITLE_TEST", "")).strip() != "SEND":
        print("ROW_TITLE_PROBE=" + json.dumps(
            {"skipped": "NAHLA_DUPLICATE_ROW_TITLE_TEST is not SEND"}, ensure_ascii=False),
            flush=True)
        return 0
    to = _first_allowlisted_recipient()
    if not to:
        print("ROW_TITLE_PROBE=" + json.dumps(
            {"skipped": "no allowlisted recipient configured"}, ensure_ascii=False), flush=True)
        return 0

    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    engine = create_engine(url, pool_pre_ping=True)
    tenant_id = int(os.environ.get("NAHLA_PROBE_TENANT_ID", "1"))
    out: Dict[str, Any] = {"tenant_id": tenant_id, "recipient_masked": _mask(to),
                           "shared_title": SHARED_TITLE, "distinct_row_ids": list(ROW_IDS)}
    with Session(engine) as session:
        try:
            out.update(asyncio.run(_run(session, tenant_id, to)))
        except Exception as exc:  # noqa: BLE001 - the outcome is the finding
            out.update({"raised": type(exc).__name__, "detail": str(exc)[:200]})
    print("ROW_TITLE_PROBE=" + json.dumps(out, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
