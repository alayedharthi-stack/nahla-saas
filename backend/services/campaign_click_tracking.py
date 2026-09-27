"""
services/campaign_click_tracking.py
───────────────────────────────────
Attribute a customer's quick-reply button tap to the campaign message it
answers — truthfully, and only where WhatsApp actually reports it.

What WhatsApp reports
─────────────────────
* **QUICK_REPLY** button on a template → the tap arrives as an inbound
  message of ``type="button"`` (or ``interactive.button_reply``) whose
  ``context.id`` is the wamid of the template message. That wamid is the
  campaign attempt's ``provider_message_id``, so the tap can be tied to
  exactly one sent copy.
* **URL / COPY_CODE / PHONE_NUMBER** buttons → no event at all. A campaign
  built on them cannot measure clicks; the dashboard shows "not available"
  rather than a misleading zero.

Rules
─────
* A tap is counted once per attempt (first tap wins; redelivered webhooks
  and repeat taps change nothing).
* Only attempts flagged ``click_trackable`` at send time count. Copies sent
  before tracking existed are matched but **not** counted — no
  retroactive attribution.
* Reads/deliveries never imply a click; only an inbound tap does.
* Best effort: this must never affect routing of the inbound message.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.campaign_click_tracking")


def extract_button_tap(msg: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """``{context_wamid, kind, inbound_id}`` when ``msg`` is a quick-reply
    tap quoting a message, else None."""
    if not isinstance(msg, dict):
        return None
    context_wamid = str(((msg.get("context") or {}).get("id")) or "").strip()
    if not context_wamid:
        return None
    mtype = str(msg.get("type") or "")
    if mtype == "button" and isinstance(msg.get("button"), dict):
        kind = "quick_reply"
    elif mtype == "interactive" and (msg.get("interactive") or {}).get("type") == "button_reply":
        kind = "quick_reply"
    else:
        return None
    return {"context_wamid": context_wamid, "kind": kind,
            "inbound_id": str(msg.get("id") or "").strip()}


def _event_time(msg: Dict[str, Any]) -> datetime:
    try:
        ts = int(msg.get("timestamp"))
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return datetime.utcnow()


def record_button_tap_from_inbound(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Record the tap against its campaign attempt in a session of its own.
    Returns a small result dict for logging, or None when the inbound is not
    a quoted button tap. Never raises."""
    tap = extract_button_tap(msg)
    if tap is None:
        return None
    try:
        from core.database import SessionLocal  # noqa: PLC0415
        from services import campaign_send_ledger as ledger  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[campaign_click] unavailable: %s", exc)
        return None
    db = SessionLocal()
    try:
        res = ledger.record_button_click(
            db, context_wamid=tap["context_wamid"], inbound_message_id=tap["inbound_id"],
            at=_event_time(msg), kind=tap["kind"],
        )
        out = {"matched": res.matched, "counted": res.counted, "reason": res.reason,
               "campaign_id": res.campaign_id, "attempt_id": res.attempt_id}
        if res.matched:
            logger.info(
                "[campaign_click] campaign=%s attempt=%s counted=%s reason=%s inbound=%s",
                res.campaign_id, res.attempt_id, res.counted, res.reason, tap["inbound_id"][-16:],
            )
        return out
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001, silent-ok — best-effort measurement must not affect routing
            pass
        logger.warning("[campaign_click] failed to record tap: %s", exc)
        return None
    finally:
        db.close()
