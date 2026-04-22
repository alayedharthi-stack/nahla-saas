"""
campaign_wizard.test_send
─────────────────────────
Real WhatsApp test-send used by Step 6 of the wizard.

Replaces the previous "simulate, return success" stub at
`POST /campaigns/test-send` so the merchant actually sees the message
on their phone before they launch the real campaign to thousands of
recipients.

Behaviour:

  * Builds a Meta-format payload (`type: "template"`) for the chosen
    template, fills body parameters from the merchant-provided variable
    map, falling back to safe MOCK_DEFAULTS for any placeholder the
    merchant left empty (so the preview never has a literal "{{1}}").
  * Sends via the canonical `provider_send_message` — same path used
    by every other transactional template send in the app (COD, cart
    recovery, …) — so any WhatsApp connectivity issue surfaces here
    in exactly the same way it would surface during launch.
  * Returns a structured result with `sent`, `wa_message_id`, and
    `error_message` so the wizard UI can render either "تم إرسال
    الاختبار، تحقّق من واتساب" or a precise reason it failed.

The function is async so FastAPI can await it directly inside the
router. It does NOT raise on send failure — it returns
`sent=False, error_message=...` because the wizard wants to keep
the merchant inside the same step instead of bouncing to a generic
error page.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import WhatsAppConnection, WhatsAppTemplate

logger = logging.getLogger(__name__)


# Demo values used when the merchant didn't fill a variable on Step 4
# yet wants to ship a test message to themselves. Kept short, neutral,
# and obviously-fake so nothing personally-identifying ends up on a
# real WhatsApp number.
MOCK_DEFAULTS: Dict[str, str] = {
    "{{1}}": "ساره",
    "{{2}}": "https://store.example/abc",
    "{{3}}": "NAHLA10",
    "{{4}}": "متجر نحلة",
    "{{5}}": "1",
    "{{6}}": "150 ر.س",
    "{{7}}": "اليوم",
    "{{8}}": "غداً",
}


def _body_text(template: WhatsAppTemplate) -> str:
    for c in (template.components or []):
        if (c.get("type") or "").upper() == "BODY":
            return c.get("text", "") or ""
    return ""


def _placeholders_in_order(text: str) -> List[str]:
    """Return every {{N}} placeholder in source order, deduped while
    preserving first occurrence. Meta requires body parameters to be
    sent in the exact order their indices appear, so we follow the
    body text scan rather than a numerically-sorted set."""
    import re
    seen: List[str] = []
    for m in re.finditer(r"\{\{\d+\}\}", text or ""):
        ph = m.group(0)
        if ph not in seen:
            seen.append(ph)
    # Meta also expects them ordered by index; sort numerically to
    # align with `extractVariables` on the frontend.
    seen.sort(key=lambda s: int(s.strip("{}")))
    return seen


def build_test_payload(
    template: WhatsAppTemplate,
    *,
    to_phone_e164: str,
    merchant_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Construct the Meta WhatsApp `type: "template"` payload. Pure
    function so we can unit-test it without any DB / network."""
    body = _body_text(template)
    placeholders = _placeholders_in_order(body)
    merchant_vars = merchant_vars or {}

    body_params: List[Dict[str, str]] = []
    for ph in placeholders:
        # Accept both "{{1}}" and bare "1" as keys — the frontend has
        # historically used the bare-number form in some places.
        val = merchant_vars.get(ph) or merchant_vars.get(ph.strip("{}")) or MOCK_DEFAULTS.get(ph) or ""
        body_params.append({"type": "text", "text": str(val)})

    components: List[Dict[str, Any]] = []
    if body_params:
        components.append({"type": "body", "parameters": body_params})

    return {
        "messaging_product": "whatsapp",
        "to":                to_phone_e164,
        "type":              "template",
        "template": {
            "name":       template.name,
            "language":   {"code": template.language or "ar"},
            "components": components,
        },
    }


async def send_test_message(
    db: Session,
    *,
    tenant_id: int,
    template_db_id: int,
    to_phone: str,
    merchant_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Top-level orchestration. Returns:

        {
          "sent":           bool,
          "simulated":      bool,    # True when no live WA connection
          "wa_message_id":  str|None,
          "to":             str,     # E.164 normalized
          "error_code":     str|None,
          "error_message":  str|None,
        }
    """
    # Inline imports keep the module load-time cheap and avoid pulling
    # the heavy whatsapp_platform graph during pytest collection of
    # tests that only exercise build_test_payload().
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    template = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id        == template_db_id,
            WhatsAppTemplate.tenant_id == tenant_id,
        )
        .first()
    )
    if not template:
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": to_phone, "error_code": "template_not_found",
            "error_message": "القالب غير موجود في حسابك.",
        }
    if (template.status or "").upper() != "APPROVED":
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": to_phone, "error_code": "template_not_approved",
            "error_message": "لا يمكن إرسال رسالة اختبار من قالب غير معتمد من Meta.",
        }

    to_e164 = normalize_phone(to_phone) or to_phone
    if not to_e164:
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": to_phone, "error_code": "invalid_recipient",
            "error_message": "رقم الجوال غير صالح — أدخل رقماً بصيغة دولية مثل +9665…",
        }

    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.tenant_id == tenant_id,
            WhatsAppConnection.status    == "connected",
        )
        .first()
    )
    if not wa_conn or not getattr(wa_conn, "phone_number_id", None):
        # Mirror the previous stub's "simulated" affordance so the
        # wizard still works in development environments where Meta
        # isn't wired up. The frontend uses `simulated=True` to render
        # a yellow info banner instead of the green "sent" toast.
        return {
            "sent": True, "simulated": True, "wa_message_id": None,
            "to": to_e164, "error_code": None,
            "error_message": "لا يوجد اتصال واتساب مفعّل — تمت محاكاة الإرسال فقط.",
        }

    payload = build_test_payload(template, to_phone_e164=to_e164, merchant_vars=merchant_vars)

    try:
        response, _ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_template",
            phone_id=wa_conn.phone_number_id,
            payload=payload,
        )
        wa_msg_id = ((response or {}).get("messages") or [{}])[0].get("id")
        return {
            "sent": True, "simulated": False, "wa_message_id": wa_msg_id,
            "to": to_e164, "error_code": None, "error_message": None,
        }
    except Exception as exc:
        # Keep the merchant inside Step 6 — return a structured failure
        # rather than re-raising. Meta's error messages are useful and
        # short enough to surface verbatim.
        logger.warning(
            "[campaign_wizard.test_send] provider_send_message failed tenant=%s tpl=%s: %s",
            tenant_id, template.name, exc,
        )
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": to_e164, "error_code": "provider_error",
            "error_message": str(exc)[:500],
        }
