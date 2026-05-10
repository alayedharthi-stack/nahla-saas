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

from models import Tenant, WhatsAppConnection, WhatsAppTemplate

from services.campaign_wizard.test_send_urls import (
    extract_button_suffix,
    resolve_test_button_url,
)

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
    """Extract the BODY text from a template's components.

    Defensive against non-dict components — older Salla / 360dialog
    payloads occasionally serialised components as raw strings inside
    ``WhatsAppTemplate.components``. We must skip those silently
    instead of raising ``AttributeError: 'str' object has no
    attribute 'get'`` mid test-send.
    """
    for c in (template.components or []):
        if not isinstance(c, dict):
            continue
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


def _dynamic_url_buttons(template: WhatsAppTemplate) -> List[Dict[str, Any]]:
    """Return a flat list of ``{index, url_template}`` for every URL
    button in the template that contains a ``{{1}}`` dynamic suffix.
    Static URL buttons (no placeholder) need no parameter and are
    skipped — Meta accepts them as-is.

    Defensive against malformed component / button entries (occasional
    string-instead-of-dict rows in legacy data) so a single bad row
    can't crash the whole test-send with ``'str' object has no
    attribute 'get'``.
    """
    out: List[Dict[str, Any]] = []
    for c in (template.components or []):
        if not isinstance(c, dict):
            continue
        if str(c.get("type") or "").upper() != "BUTTONS":
            continue
        buttons = c.get("buttons") or []
        if not isinstance(buttons, list):
            continue
        for idx, btn in enumerate(buttons):
            if not isinstance(btn, dict):
                continue
            if str(btn.get("type") or "").upper() != "URL":
                continue
            url_tpl = btn.get("url") or ""
            if "{{1}}" in url_tpl:
                out.append({"index": idx, "url_template": url_tpl})
    return out


def _coerce_merchant_vars(merchant_vars: Any) -> Dict[str, str]:
    """Normalise the merchant-supplied variable map.

    Defensive: the wizard's Step 4 emits a flat ``{name -> string}``
    dict, but the legacy ``/campaigns/test-send`` route and a couple
    of older callers used to pass:

      * a JSON string of a dict
      * a list of ``{key, value}`` pairs
      * ``None``

    Returning a clean ``Dict[str, str]`` here means the rest of the
    pipeline (``_first_non_empty``, ``MOCK_DEFAULTS`` lookup) can stay
    simple and never raise on a list/None/scalar value.
    """
    if not merchant_vars:
        return {}
    if isinstance(merchant_vars, dict):
        out: Dict[str, str] = {}
        for k, v in merchant_vars.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float)):
                out[str(k)] = str(v)
            # Lists / dicts inside variables are merchant input mistakes —
            # skip them rather than letting them poison the body params.
        return out
    if isinstance(merchant_vars, list):
        out = {}
        for item in merchant_vars:
            if isinstance(item, dict):
                k = item.get("key") or item.get("name") or item.get("placeholder")
                v = item.get("value") or item.get("text")
                if k and v is not None:
                    out[str(k)] = str(v)
        return out
    if isinstance(merchant_vars, str):
        try:
            import json as _json  # noqa: PLC0415
            parsed = _json.loads(merchant_vars)
            return _coerce_merchant_vars(parsed)
        except Exception:
            return {}
    return {}


def _coerce_recipient(recipient: Any) -> tuple[str, str]:
    """Accept either a plain phone string or a ``{phone, name}`` dict.

    The wizard's frontend only sends plain strings today, but the
    legacy campaigns API has historically accepted both shapes — and
    a couple of older mobile clients still post the dict form.
    Returns ``(phone, name)`` with sensible defaults; raises
    ``ValueError`` only when nothing usable was supplied.
    """
    if recipient is None:
        raise ValueError("لم يُرفق رقم اختبار")
    if isinstance(recipient, str):
        phone = recipient.strip()
        if not phone:
            raise ValueError("رقم الاختبار فارغ")
        return phone, "اختبار"
    if isinstance(recipient, dict):
        phone = (
            recipient.get("phone")
            or recipient.get("mobile")
            or recipient.get("number")
            or recipient.get("to_phone")
            or recipient.get("to")
            or ""
        )
        if not isinstance(phone, str) or not phone.strip():
            raise ValueError("الرقم داخل بيانات الاختبار غير صالح")
        name = recipient.get("name") or recipient.get("customer_name") or "اختبار"
        return str(phone).strip(), str(name)
    if isinstance(recipient, (int, float)):
        return str(recipient), "اختبار"
    raise ValueError(f"شكل بيانات الاختبار غير مدعوم: {type(recipient).__name__}")


def build_test_payload(
    template: WhatsAppTemplate,
    *,
    to_phone_e164: str,
    merchant_vars: Optional[Dict[str, str]] = None,
    store_domain_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct the Meta WhatsApp `type: "template"` payload. Pure
    function so we can unit-test it without any DB / network.

    ``store_domain_hint`` is only consulted by the URL-button resolver
    when the merchant didn't supply *any* usable URL var. It is a
    convenience for the test-send orchestrator, which derives it from
    ``Tenant.domain``."""
    body = _body_text(template)
    placeholders = _placeholders_in_order(body)
    merchant_vars = _coerce_merchant_vars(merchant_vars)

    body_params: List[Dict[str, str]] = []
    for ph in placeholders:
        # Accept both "{{1}}" and bare "1" as keys — the frontend has
        # historically used the bare-number form in some places.
        val = merchant_vars.get(ph) or merchant_vars.get(ph.strip("{}")) or MOCK_DEFAULTS.get(ph) or ""
        body_params.append({"type": "text", "text": str(val)})

    components: List[Dict[str, Any]] = []
    if body_params:
        components.append({"type": "body", "parameters": body_params})

    # ── Dynamic URL buttons (test-send only fallback chain) ────────────────
    # Salla sandbox cart URLs frequently 404 / show maintenance — the test
    # path therefore tolerates a missing cart_url and walks a documented
    # fallback chain. PRODUCTION cart-recovery sends still go through
    # `core/automation_engine.py` which deliberately fails closed when no
    # real cart_url is present.
    for btn_meta in _dynamic_url_buttons(template):
        resolved_url, source = resolve_test_button_url(
            merchant_vars, store_domain_hint=store_domain_hint,
        )
        suffix = extract_button_suffix(btn_meta["url_template"], resolved_url)
        if not suffix:
            # extract_button_suffix only returns "" for an empty input;
            # belt-and-braces guard so we never emit a button parameter
            # of "" which Meta rejects with code 132000.
            suffix = "preview/test"
        logger.info(
            "[test_send] URL button idx=%s template_url=%s resolved=%s "
            "(source=%s) suffix=%s",
            btn_meta["index"], btn_meta["url_template"], resolved_url,
            source, suffix,
        )
        components.append({
            "type":       "button",
            "sub_type":   "url",
            "index":      str(btn_meta["index"]),
            "parameters": [{"type": "text", "text": suffix}],
        })

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
    to_phone: Any,
    merchant_vars: Optional[Any] = None,
) -> Dict[str, Any]:
    """Public entry point — wraps :func:`_send_test_message_inner` in a
    final catch-all so any regression that leaks a raw Python error
    (e.g. the historical ``'str' object has no attribute 'get'``) is
    converted to a friendly Arabic message before reaching the merchant.

    The inner function returns structured failures for every *known*
    failure mode; this wrapper exists only as a safety net for the
    *unknown* ones.
    """
    try:
        return await _send_test_message_inner(
            db,
            tenant_id=tenant_id,
            template_db_id=template_db_id,
            to_phone=to_phone,
            merchant_vars=merchant_vars,
        )
    except Exception as exc:
        logger.exception(
            "[CAMPAIGN_TEST_SEND] unexpected error tenant=%s tpl_id=%s: %s",
            tenant_id, template_db_id, exc,
        )
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": str(to_phone)[:64] if to_phone is not None else "",
            "error_code": "unexpected_error",
            "error_message": (
                "تعذّر إرسال رسالة الاختبار بسبب خطأ غير متوقع. "
                "تواصل مع الدعم إذا استمرت المشكلة."
            ),
        }


async def _send_test_message_inner(
    db: Session,
    *,
    tenant_id: int,
    template_db_id: int,
    to_phone: Any,
    merchant_vars: Optional[Any] = None,
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

    ``to_phone`` may be a plain string (``"0542980511"``) OR a dict
    of the legacy shape ``{"phone": "0542980511", "name": "اختبار"}``.
    Any other shape is rejected with a structured error rather than a
    raw ``AttributeError``.

    ``merchant_vars`` is similarly tolerant — see
    :func:`_coerce_merchant_vars`. Together these two guards make
    test-send a defensive citizen so a sloppy frontend payload never
    bubbles up as ``'str' object has no attribute 'get'``.
    """
    # Coerce the recipient ASAP so all downstream logging / observability
    # uses the normalised phone string.
    try:
        raw_phone, recipient_name = _coerce_recipient(to_phone)
        recipient_type = type(to_phone).__name__
    except Exception as exc:
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": str(to_phone)[:64], "error_code": "invalid_recipient_shape",
            "error_message": str(exc) or "بيانات الاختبار غير صالحة",
        }

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
            "to": raw_phone, "error_code": "template_not_found",
            "error_message": "القالب غير موجود في حسابك.",
        }
    if (template.status or "").upper() != "APPROVED":
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": raw_phone, "error_code": "template_not_approved",
            "error_message": "لا يمكن إرسال رسالة اختبار من قالب غير معتمد من Meta.",
        }

    to_e164 = normalize_phone(raw_phone) or raw_phone
    if not to_e164:
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": raw_phone, "error_code": "invalid_recipient",
            "error_message": "رقم الجوال غير صالح — أدخل رقماً بصيغة دولية مثل +9665…",
        }

    # Detailed pre-send logging. Phone numbers are masked, variable
    # values are summarised to types (NEVER values) so PII never lands
    # in logs while still giving us enough signal to debug shape bugs
    # like "merchant sent variables as a string" or "components has a
    # string entry instead of a dict".
    coerced_vars = _coerce_merchant_vars(merchant_vars) or {}
    raw_components = template.components
    components_kind = (
        "list" if isinstance(raw_components, list)
        else "dict" if isinstance(raw_components, dict)
        else "str" if isinstance(raw_components, str)
        else type(raw_components).__name__
    )
    component_item_kinds = (
        [type(c).__name__ for c in raw_components]
        if isinstance(raw_components, list) else None
    )
    var_value_types = {k: type(v).__name__ for k, v in (coerced_vars or {}).items()}
    logger.info(
        "[CAMPAIGN_TEST_SEND] tenant_id=%s template_id=%s template_name=%s "
        "recipient_type=%s phone_masked=%s phone_normalized=%s "
        "vars_count=%d vars_value_types=%s recipient_name=%r "
        "components_kind=%s components_item_kinds=%s template_status=%s",
        tenant_id, template_db_id, getattr(template, "name", None),
        recipient_type,
        (raw_phone[:4] + "***" + raw_phone[-2:]) if len(raw_phone) >= 6 else "***",
        to_e164, len(coerced_vars), var_value_types, recipient_name,
        components_kind, component_item_kinds, template.status,
    )

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

    # Pull the tenant's storefront domain so the URL-button resolver
    # has a sensible last-resort target when neither cart_url nor any
    # other *_url variable was supplied (typical Salla sandbox case).
    store_domain_hint: Optional[str] = None
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if tenant and getattr(tenant, "domain", None):
            store_domain_hint = str(tenant.domain).strip() or None
    except Exception as exc:  # observability only — never block test-send
        logger.debug("[test_send] failed to read tenant domain: %s", exc)

    try:
        payload = build_test_payload(
            template,
            to_phone_e164=to_e164,
            merchant_vars=merchant_vars,
            store_domain_hint=store_domain_hint,
        )
    except Exception as exc:
        logger.warning(
            "[CAMPAIGN_TEST_SEND] build_test_payload failed tenant=%s tpl=%s: %s",
            tenant_id, getattr(template, "name", None), exc, exc_info=True,
        )
        return {
            "sent": False, "simulated": False, "wa_message_id": None,
            "to": to_e164, "error_code": "payload_build_failed",
            "error_message": (
                "تعذر تجهيز رسالة الاختبار من القالب — تحقق من بيانات القالب أو أعد إنشاءه."
            ),
        }

    try:
        response, _ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_template",
            phone_id=wa_conn.phone_number_id,
            payload=payload,
        )
        # NOTE: provider_post_with_context does *not* raise on a non-2xx
        # response — it just returns the parsed JSON body. That means a
        # Meta validation error (e.g. "Template name does not exist in
        # the translation", code 132001) comes back as
        # `{"error": {...}}` and was previously treated as success
        # because we only looked for `messages[0].id`. Surface that as
        # a real failure so the merchant sees the actual reason instead
        # of a green "تم الإرسال — تحقّق من واتساب" toast for a message
        # that will never arrive.
        resp = response or {}
        meta_err = resp.get("error") if isinstance(resp, dict) else None
        if meta_err:
            # Defensive: Meta usually returns ``error`` as a structured
            # dict (`{"message", "code", "error_user_msg", ...}`), but a
            # CDN/proxy in front of the Graph API can occasionally return
            # ``{"error": "Bad Gateway"}`` with a bare string. Calling
            # ``.get(...)`` on the string raises
            # ``'str' object has no attribute 'get'``, which previously
            # surfaced verbatim in the wizard UI. Treat both shapes.
            if isinstance(meta_err, dict):
                err_msg = (meta_err.get("error_user_msg")
                           or meta_err.get("message")
                           or "فشل الإرسال من Meta.")
                err_code = meta_err.get("code") or meta_err.get("type") or "meta_error"
            else:
                err_msg = str(meta_err) or "فشل الإرسال من Meta."
                err_code = "meta_error"
            logger.warning(
                "[campaign_wizard.test_send] Meta returned error tenant=%s tpl=%s code=%s msg=%s",
                tenant_id, template.name, err_code, err_msg,
            )
            return {
                "sent": False, "simulated": False, "wa_message_id": None,
                "to": to_e164, "error_code": f"meta:{err_code}",
                "error_message": str(err_msg)[:500],
            }

        # ``messages`` is expected to be ``[{"id": "wamid..."}]``; tolerate
        # ``[{...wrong shape...}]`` or ``["string"]`` so we never raise
        # AttributeError here either.
        messages = resp.get("messages") if isinstance(resp, dict) else None
        first_msg = messages[0] if isinstance(messages, list) and messages else None
        wa_msg_id = first_msg.get("id") if isinstance(first_msg, dict) else None
        if not wa_msg_id:
            # Meta accepted no error and gave no message id — treat as
            # failure so we never lie to the merchant. Include the raw
            # response keys to help debugging without leaking tokens.
            logger.warning(
                "[campaign_wizard.test_send] no message id in Meta response tenant=%s tpl=%s keys=%s",
                tenant_id, template.name,
                list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__,
            )
            return {
                "sent": False, "simulated": False, "wa_message_id": None,
                "to": to_e164, "error_code": "no_message_id",
                "error_message": "لم يُرجع واتساب معرّف رسالة — قد يكون الرقم غير مسجّل في واتساب أو القالب غير معتمد لهذه اللغة.",
            }

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
