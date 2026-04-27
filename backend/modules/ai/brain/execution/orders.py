"""
brain/execution/orders.py
──────────────────────────
DraftOrderHandler: executes ACTION_PROPOSE_DRAFT_ORDER.

Creates a draft order in the merchant's store (via order_service) and
returns the checkout URL. Falls back to a WhatsApp-friendly "intent
captured" message when no store adapter is available (e.g. store not
connected or adapter doesn't support draft orders).
"""
from __future__ import annotations

import logging
import os, sys

logger = logging.getLogger("nahla.brain.execution.orders")

_THIS    = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, "../../../../.."))
_DB      = os.path.abspath(os.path.join(_BACKEND, "../database"))
for _p in (_BACKEND, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ...brain.types import (
    ActionResult,
    BrainContext,
    Decision,
    OrderPreparationState,
)
from services.address_resolution import (
    extract_address_signals,
    resolve_coordinates,
    resolve_short_address,
    spl_resolution_available,
)


class DraftOrderHandler:
    """Handles ACTION_PROPOSE_DRAFT_ORDER."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime

        product_info = decision.args.get("product") or ctx.state.current_product_focus
        if not product_info:
            return ActionResult(
                success=False,
                error="no_product_focus",
                data={"message": "no_product_selected"},
            )

        # Load prep from state.
        # Reset address ONLY when the product changes — not on every Salla failure.
        # Failure retry is handled below: first failure → keep data + retry message;
        # second consecutive failure → clear address + ask to re-enter.
        prev_prep = ctx.state.order_prep or OrderPreparationState()
        prep = OrderPreparationState.from_dict(prev_prep.to_dict())

        current_product_id = str(product_info.get("external_id") or product_info.get("id") or "")
        previous_product_id = str(getattr(prev_prep, "product_id", "") or "")
        product_changed = bool(current_product_id and previous_product_id and current_product_id != previous_product_id)
        previous_failed = bool(getattr(prev_prep, "last_order_failed", False))

        if product_changed:
            logger.info(
                "[DraftOrderHandler] Product changed — resetting address | "
                "tenant=%s old=%s new=%s",
                ctx.tenant_id, previous_product_id, current_product_id,
            )
            prep.short_address_code = ""
            prep.google_maps_url = ""
            prep.latitude = None
            prep.longitude = None
            prep.street = ""
            prep.district = ""
            prep.postal_code = ""
            prep.building_number = ""
            prep.additional_number = ""
            prep.address_line = ""
            prep.resolution_source = ""
            prep.last_order_failed = False

        # Track which product this prep belongs to
        prep.product_id = current_product_id

        # is_first_ask: True when no customer data exists yet (very first data-collection turn)
        _is_first_ask = not bool(prep.customer_first_name or prep.city or prep.short_address_code)

        _seed_checkout_state(prep, ctx)
        _merge_message_details(prep, ctx.intent.slots, ctx.message)
        await _resolve_checkout_address(prep)

        # Country-aware address rules: Saudi customers can finish the
        # order with name + city + (national short code OR Maps URL).
        # International customers need an explicit country and either
        # a structured address or a free-form address line. The phone
        # E.164 prefix is the source of truth for "is the customer in SA".
        is_sa = _is_saudi_customer(ctx.customer_phone)
        missing = _missing_checkout_fields(prep, is_sa=is_sa)
        prep.missing_fields = missing
        if missing:
            return ActionResult(
                success=True,
                data={
                    "product": product_info,
                    "needs_collection": True,
                    "missing_fields": missing,
                    "question": _checkout_question(missing[0], is_sa=is_sa),
                    "is_first_ask": _is_first_ask,
                    "order_prep": prep.to_dict(),
                    "resolution_available": spl_resolution_available(),
                    "customer_region": "SA" if is_sa else "INTL",
                },
            )

        external_id = product_info.get("external_id") or str(product_info.get("id", ""))
        if not external_id:
            logger.error(
                "[ORDER FLOW] No product_id | tenant=%s product_info=%s",
                ctx.tenant_id, product_info,
            )
            return ActionResult(
                success=False,
                error="missing_product_id",
                data={"message": "product_has_no_external_id"},
            )

        # Log phone resolution — phone is always taken from the WhatsApp conversation,
        # never asked from the customer.
        _resolved_phone = ctx.customer_phone or ""
        logger.info(
            "[ORDER FLOW] phone resolved from conversation | phone=%s tenant=%s",
            _resolved_phone, ctx.tenant_id,
        )
        logger.info(
            "[ORDER FLOW] All data collected → creating order | tenant=%s "
            "product=%s external_id=%s name=%r phone=%s city=%r "
            "short_code=%r has_maps=%s quantity=%d previous_failed=%s",
            ctx.tenant_id,
            product_info.get("title", "?"),
            external_id,
            (prep.customer_first_name + " " + prep.customer_last_name).strip(),
            _resolved_phone[-4:] if _resolved_phone else "????",
            prep.city,
            prep.short_address_code,
            bool(prep.google_maps_url),
            max(int(prep.quantity or 1), 1),
            previous_failed,
        )

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        runtime_result = await runtime.execute(
            "create_draft_order",
            {
                "product_id": external_id,
                "quantity": max(int(prep.quantity or 1), 1),
                "customer_name": _full_name(prep, ctx.profile.get("name", "عميل")),
                "customer_email": prep.customer_email or ctx.profile.get("email"),
                "customer_first_name": prep.customer_first_name,
                "customer_last_name": prep.customer_last_name,
                "building_number": prep.building_number,
                "additional_number": prep.additional_number,
                "street": prep.street,
                "district": prep.district,
                "postal_code": prep.postal_code,
                "city": prep.city,
                "address": _address_line(prep),
                "short_address_code": prep.short_address_code,
                "google_maps_url": prep.google_maps_url,
                "latitude": _safe_float(prep.latitude),
                "longitude": _safe_float(prep.longitude),
                "payment_method": "online",
                "notes": _build_order_notes(prep),
            },
        )
        order = runtime_result.payload.get("order")

        if order:
            checkout_url = order.get("payment_link") or order.get("checkout_url") or ""
            logger.info(
                "[ORDER FLOW] Order created ✓ | tenant=%s product=%s order_id=%s checkout=%s",
                ctx.tenant_id,
                product_info.get("title", "?"),
                order.get("id"),
                "YES" if checkout_url else "NO",
            )
            return ActionResult(
                success=True,
                data={
                    "order_id":    order.get("id"),
                    "reference":   order.get("reference_id") or order.get("id"),
                    "checkout_url": checkout_url,
                    "total":       order.get("total"),
                    "currency":    order.get("currency", "SAR"),
                    "product":     product_info,
                    "order_prep":  prep.to_dict(),
                },
            )

        # ── Order creation FAILED ─────────────────────────────────────────────
        error_msg = str(runtime_result.error or "unknown")
        logger.error(
            "[ORDER FLOW] Order creation FAILED ✗ | tenant=%s product=%s "
            "external_id=%s error=%r name=%r city=%r short_code=%r "
            "previous_failed=%s",
            ctx.tenant_id,
            product_info.get("title", "?"),
            external_id,
            error_msg,
            prep.customer_first_name,
            prep.city,
            prep.short_address_code,
            previous_failed,
        )

        if prep.customer_first_name and prep.city:
            has_address = bool(prep.short_address_code or prep.google_maps_url or prep.latitude)

            if has_address and not previous_failed:
                # ── First failure with address present → keep data, show retry message ──
                # The customer already provided a valid-looking address code.
                # Don't ask for it again — just tell them we'll retry.
                prep.last_order_failed = True
                prep.missing_fields = []
                return ActionResult(
                    success=True,
                    data={
                        "product": product_info,
                        "salla_retry": True,
                        "salla_address_code": prep.short_address_code,
                        "order_prep": prep.to_dict(),
                        "order_creation_error": error_msg,
                    },
                )

            # ── Second consecutive failure OR no address → clear address and re-ask ──
            # This covers: retry-after-first-failure also failed, or address was
            # never sent and the issue might be something else entirely.
            prep.last_order_failed = True
            prep.short_address_code = ""
            prep.google_maps_url = ""
            prep.latitude = None
            prep.longitude = None
            prep.street = ""
            prep.district = ""
            prep.postal_code = ""
            prep.building_number = ""
            prep.additional_number = ""
            prep.address_line = ""
            prep.missing_fields = ["address_location"]
            question = (
                "واجهنا مشكلة مرتين في إنشاء الطلب 🙏\n"
                "هل يمكنك إعادة إرسال الرمز الوطني المختصر (مثال: RIYD1234) "
                "أو رابط موقعك من خرائط جوجل؟"
                if previous_failed
                else
                "واجهنا مشكلة تقنية في إنشاء الطلب 🙏\n"
                "هل يمكنك إرسال الرمز الوطني المختصر للعنوان (مثال: RIYD1234) "
                "أو رابط موقعك من خرائط جوجل؟"
            )
            return ActionResult(
                success=True,
                data={
                    "product": product_info,
                    "needs_collection": True,
                    "missing_fields": ["address_location"],
                    "question": question,
                    "is_first_ask": False,
                    "order_prep": prep.to_dict(),
                    "order_creation_error": error_msg,
                },
            )

        # No name/city at all — adapter missing or completely fresh start
        return ActionResult(
            success=True,
            data={
                "order_id":    None,
                "checkout_url": "",
                "product":     product_info,
                "intent_only": True,
                "order_creation_error": error_msg,
                "order_prep":  prep.to_dict(),
            },
        )


class TrackOrderHandler:
    """Handles ACTION_TRACK_ORDER."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        runtime_result = await runtime.execute("track_order", {})
        latest = runtime_result.payload.get("order") if runtime_result.ok else None

        if not latest:
            return ActionResult(
                success=False,
                error="no_orders",
                data={"message": "no_orders_found"},
            )

        return ActionResult(
            success=True,
            data={
                "order_id":  latest.get("id"),
                "reference": latest.get("reference_id") or latest.get("id"),
                "status":    latest.get("status"),
                "total":     latest.get("total"),
                "currency":  latest.get("currency", "SAR"),
            },
        )


def _seed_checkout_state(prep: OrderPreparationState, ctx: BrainContext) -> None:
    full_name = str(ctx.profile.get("name") or "").strip()
    first, last = _split_name(full_name)
    if not prep.customer_first_name and first:
        prep.customer_first_name = first
    if not prep.customer_last_name and last:
        prep.customer_last_name = last
    if not prep.customer_email:
        prep.customer_email = str(ctx.profile.get("email") or "").strip()


def _merge_message_details(prep: OrderPreparationState, slots: dict, message: str) -> None:
    slots = slots or {}

    quantity = _to_int(slots.get("quantity"))
    if quantity:
        prep.quantity = max(quantity, 1)

    full_name = str(slots.get("customer_name") or slots.get("full_name") or "").strip()
    first_name = str(slots.get("customer_first_name") or slots.get("first_name") or "").strip()
    last_name = str(slots.get("customer_last_name") or slots.get("last_name") or "").strip()
    city = str(slots.get("city") or "").strip()
    country = str(slots.get("country") or "").strip()
    email = str(slots.get("customer_email") or slots.get("email") or "").strip()
    short_code = str(slots.get("short_address_code") or "").strip().upper()
    maps_url = str(slots.get("google_maps_url") or slots.get("location_url") or "").strip()
    address_line = str(slots.get("address_line") or slots.get("address") or "").strip()
    street = str(slots.get("street") or "").strip()
    district = str(slots.get("district") or "").strip()
    postal_code = str(slots.get("postal_code") or slots.get("zip_code") or "").strip()
    building_number = str(slots.get("building_number") or "").strip()
    additional_number = str(slots.get("additional_number") or "").strip()

    if full_name and not (first_name or last_name):
        first_name, last_name = _split_name(full_name)

    if first_name:
        prep.customer_first_name = first_name
    if last_name:
        prep.customer_last_name = last_name
    if city:
        prep.city = city
    if country:
        prep.country = country
    if email:
        prep.customer_email = email
    if short_code:
        prep.short_address_code = short_code
    if maps_url:
        prep.google_maps_url = maps_url
    if address_line:
        prep.address_line = address_line
    if street:
        prep.street = street
    if district:
        prep.district = district
    if postal_code:
        prep.postal_code = postal_code
    if building_number:
        prep.building_number = building_number
    if additional_number:
        prep.additional_number = additional_number

    signals = extract_address_signals(message)
    if signals.get("short_address_code") and not prep.short_address_code:
        prep.short_address_code = str(signals["short_address_code"]).upper()
    if signals.get("google_maps_url") and not prep.google_maps_url:
        prep.google_maps_url = str(signals["google_maps_url"])
    if signals.get("latitude") is not None and prep.latitude is None:
        prep.latitude = _safe_float(signals.get("latitude"))
    if signals.get("longitude") is not None and prep.longitude is None:
        prep.longitude = _safe_float(signals.get("longitude"))

    if "latitude" in slots and prep.latitude is None:
        prep.latitude = _safe_float(slots.get("latitude"))
    if "longitude" in slots and prep.longitude is None:
        prep.longitude = _safe_float(slots.get("longitude"))


async def _resolve_checkout_address(prep: OrderPreparationState) -> None:
    if prep.short_address_code and not _has_structured_address(prep):
        resolved = await resolve_short_address(prep.short_address_code, city=prep.city)
        _merge_resolved_address(prep, resolved)

    if (prep.latitude is not None and prep.longitude is not None) and (
        not _has_structured_address(prep) or not prep.city
    ):
        resolved = await resolve_coordinates(prep.latitude, prep.longitude)
        _merge_resolved_address(prep, resolved)


def _merge_resolved_address(
    prep: OrderPreparationState,
    resolved: object,
) -> None:
    if not resolved:
        return

    city = str(getattr(resolved, "city", "") or "").strip()
    district = str(getattr(resolved, "district", "") or "").strip()
    street = str(getattr(resolved, "street", "") or "").strip()
    postal_code = str(getattr(resolved, "postal_code", "") or "").strip()
    building_number = str(getattr(resolved, "building_number", "") or "").strip()
    additional_number = str(getattr(resolved, "additional_number", "") or "").strip()
    short_code = str(getattr(resolved, "short_address_code", "") or "").strip().upper()
    maps_url = str(getattr(resolved, "google_maps_url", "") or "").strip()
    resolution_source = str(getattr(resolved, "resolution_source", "") or "").strip()
    lat = _safe_float(getattr(resolved, "latitude", None))
    lng = _safe_float(getattr(resolved, "longitude", None))

    if city and not prep.city:
        prep.city = city
    if district and not prep.district:
        prep.district = district
    if street and not prep.street:
        prep.street = street
    if postal_code and not prep.postal_code:
        prep.postal_code = postal_code
    if building_number and not prep.building_number:
        prep.building_number = building_number
    if additional_number and not prep.additional_number:
        prep.additional_number = additional_number
    if short_code and not prep.short_address_code:
        prep.short_address_code = short_code
    if maps_url and not prep.google_maps_url:
        prep.google_maps_url = maps_url
    if lat is not None and prep.latitude is None:
        prep.latitude = lat
    if lng is not None and prep.longitude is None:
        prep.longitude = lng
    if resolution_source:
        prep.resolution_source = resolution_source


def _missing_checkout_fields(
    prep: OrderPreparationState,
    *,
    is_sa: bool = True,
) -> list[str]:
    """
    Checkout requirements differ by region:

      Saudi customers (default):
        - first name + last name (or single full name)
        - city
        - SHORT national address code  OR  Google Maps URL
          (no need to grill them about district/street/postal)

      International customers:
        - first name + last name
        - country (free text or detected from phone)
        - city
        - a structured address OR an explicit free-form address_line
    """
    missing: list[str] = []
    if not prep.customer_first_name:
        missing.append("customer_first_name")
    if not prep.customer_last_name:
        missing.append("customer_last_name")
    if not prep.city:
        missing.append("city")

    if is_sa:
        if not _has_sa_checkout_address(prep):
            missing.append("address_location")
    else:
        if not _has_intl_country(prep):
            missing.append("country")
        if not _has_intl_address(prep):
            missing.append("address_line")

    return missing


def _has_structured_address(prep: OrderPreparationState) -> bool:
    return bool(prep.street and prep.district and prep.postal_code)


def _has_sa_checkout_address(prep: OrderPreparationState) -> bool:
    """SA flow: short code OR maps URL is enough; structured address still
    counts as a complete answer if the customer happened to send it."""
    return bool(
        prep.short_address_code
        or prep.google_maps_url
        or (prep.latitude is not None and prep.longitude is not None)
        or _has_structured_address(prep)
        or prep.address_line
    )


def _has_intl_address(prep: OrderPreparationState) -> bool:
    """International flow: free-form address line OR a fully-structured one."""
    return bool(prep.address_line or _has_structured_address(prep))


def _has_intl_country(prep: OrderPreparationState) -> bool:
    return bool(getattr(prep, "country", "") or getattr(prep, "country_code", ""))


def _checkout_question(field_name: str, *, is_sa: bool = True) -> str:
    if is_sa:
        questions = {
            "customer_first_name": "ممتاز، ما اسمك الأول لإكمال الطلب؟",
            "customer_last_name": "وما اسم العائلة كما يظهر في عنوان التسليم؟",
            "city": "ما المدينة التي سيصلها الطلب؟",
            "address_location": (
                "أرسل الرمز الوطني المختصر للعقار، أو رابط موقعك من Google Maps "
                "وسأجهّز الطلب فوراً."
            ),
        }
    else:
        questions = {
            "customer_first_name": "Could I have your first name to start the order?",
            "customer_last_name":  "And your last name (as it should appear on the delivery)?",
            "country":             "Which country should we ship to?",
            "city":                "Which city should we ship to?",
            "address_line": (
                "Please share the full delivery address (building / street / "
                "district / postal code, or any landmark). You can also paste a "
                "Google Maps link if that's easier."
            ),
        }
    return questions.get(field_name, "أرسل لي التفاصيل الناقصة لإكمال الطلب.")


def _is_saudi_customer(customer_phone: str | None) -> bool:
    """
    True when the customer's phone is a Saudi (+966) E.164 number.
    Falls back to True (SA-first product strategy) when the phone is empty
    or unparseable, so we never accidentally inflict the long international
    flow on a customer the system simply couldn't classify.
    """
    raw = (customer_phone or "").strip()
    if not raw:
        return True

    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+966") or digits.startswith("966"):
        return True

    # Any explicit "+<other-country>" prefix means international flow,
    # regardless of whether libphonenumber is available to normalise it.
    if raw.startswith("+"):
        return False

    # Try libphonenumber for ambiguous shapes like "971501234567".
    try:
        from services.customer_intelligence import normalize_phone as _normalize
        e164 = _normalize(raw) or ""
        if e164.startswith("+966"):
            return True
        if e164.startswith("+"):
            return False
    except Exception:
        pass

    # Local-format "05xxxxxxxx" — almost always Saudi in this product context.
    return True


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [part.strip() for part in (full_name or "").split() if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _full_name(prep: OrderPreparationState, fallback: str) -> str:
    parts = [prep.customer_first_name.strip(), prep.customer_last_name.strip()]
    name = " ".join(part for part in parts if part)
    return name or str(fallback or "عميل").strip() or "عميل"


def _address_line(prep: OrderPreparationState) -> str:
    if prep.address_line:
        return prep.address_line
    if prep.street:
        suffix = f" - {prep.district}" if prep.district else ""
        return f"{prep.street}{suffix}".strip()
    if prep.short_address_code:
        return prep.short_address_code
    if prep.google_maps_url:
        return "تم تزويد الموقع عبر خرائط Google"
    return ""


def _build_order_notes(prep: OrderPreparationState) -> str:
    lines = ["طلب أنشأه نظام نحلة الذكي عبر واتساب"]
    if prep.short_address_code:
        lines.append(f"الرمز الوطني المختصر: {prep.short_address_code}")
    if prep.google_maps_url:
        lines.append(f"رابط الموقع: {prep.google_maps_url}")
    if prep.resolution_source:
        lines.append(f"مصدر حل العنوان: {prep.resolution_source}")
    if prep.additional_number:
        lines.append(f"الرقم الإضافي: {prep.additional_number}")
    return " | ".join(lines)


def _to_int(value: object) -> int:
    try:
        if value in (None, "", 0):
            return 0
        return int(value)
    except Exception:
        return 0


def _safe_float(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None
