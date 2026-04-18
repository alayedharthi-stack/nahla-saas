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
        from store_integration.models import OrderInput, OrderItemInput
        from store_integration.order_service import create_draft_order

        product_info = decision.args.get("product") or ctx.state.current_product_focus
        if not product_info:
            return ActionResult(
                success=False,
                error="no_product_focus",
                data={"message": "no_product_selected"},
            )

        prep = OrderPreparationState.from_dict((ctx.state.order_prep or OrderPreparationState()).to_dict())
        _seed_checkout_state(prep, ctx)
        _merge_message_details(prep, ctx.intent.slots, ctx.message)
        await _resolve_checkout_address(prep)

        missing = _missing_checkout_fields(prep)
        prep.missing_fields = missing
        if missing:
            return ActionResult(
                success=True,
                data={
                    "product": product_info,
                    "needs_collection": True,
                    "missing_fields": missing,
                    "question": _checkout_question(missing[0]),
                    "order_prep": prep.to_dict(),
                    "resolution_available": spl_resolution_available(),
                },
            )

        external_id = product_info.get("external_id") or str(product_info.get("id", ""))
        if not external_id:
            return ActionResult(
                success=False,
                error="missing_product_id",
                data={"message": "product_has_no_external_id"},
            )

        order_input = OrderInput(
            customer_name=_full_name(prep, ctx.profile.get("name", "عميل")),
            customer_phone=ctx.customer_phone,
            customer_email=prep.customer_email or ctx.profile.get("email"),
            customer_first_name=prep.customer_first_name,
            customer_last_name=prep.customer_last_name,
            building_number=prep.building_number,
            additional_number=prep.additional_number,
            street=prep.street,
            district=prep.district,
            postal_code=prep.postal_code,
            city=prep.city,
            address=_address_line(prep),
            short_address_code=prep.short_address_code,
            google_maps_url=prep.google_maps_url,
            latitude=_safe_float(prep.latitude),
            longitude=_safe_float(prep.longitude),
            payment_method="online",
            items=[OrderItemInput(product_id=external_id, quantity=max(int(prep.quantity or 1), 1))],
            notes=_build_order_notes(prep),
        )

        try:
            order = await create_draft_order(ctx.tenant_id, order_input)
        except Exception as exc:
            logger.warning("[DraftOrderHandler] create_draft_order error: %s", exc)
            order = None

        if order:
            return ActionResult(
                success=True,
                data={
                    "order_id":    order.id,
                    "reference":   order.reference_id or order.id,
                    "checkout_url": order.payment_link or "",
                    "total":       order.total,
                    "currency":    order.currency,
                    "product":     product_info,
                    "order_prep":  prep.to_dict(),
                },
            )

        # No adapter / adapter failed — record intent for follow-up
        logger.info(
            "[DraftOrderHandler] tenant=%s — no adapter or draft failed, recording intent",
            ctx.tenant_id,
        )
        return ActionResult(
            success=True,   # success=True so composer produces a friendly reply
            data={
                "order_id":    None,
                "checkout_url": "",
                "product":     product_info,
                "intent_only": True,
                "order_prep":  prep.to_dict(),
            },
        )


class TrackOrderHandler:
    """Handles ACTION_TRACK_ORDER."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from store_integration.order_service import get_customer_orders

        try:
            orders = await get_customer_orders(ctx.tenant_id, ctx.customer_phone)
        except Exception as exc:
            logger.warning("[TrackOrderHandler] error: %s", exc)
            orders = []

        if not orders:
            return ActionResult(
                success=False,
                error="no_orders",
                data={"message": "no_orders_found"},
            )

        latest = orders[0]
        return ActionResult(
            success=True,
            data={
                "order_id":  latest.id,
                "reference": latest.reference_id or latest.id,
                "status":    latest.status,
                "total":     latest.total,
                "currency":  latest.currency,
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


def _missing_checkout_fields(prep: OrderPreparationState) -> list[str]:
    missing: list[str] = []
    if not prep.customer_first_name:
        missing.append("customer_first_name")
    if not prep.customer_last_name:
        missing.append("customer_last_name")
    if not prep.city:
        missing.append("city")
    if not _has_checkout_address(prep):
        missing.append("address_location")
    return missing


def _has_structured_address(prep: OrderPreparationState) -> bool:
    return bool(prep.street and prep.district and prep.postal_code)


def _has_checkout_address(prep: OrderPreparationState) -> bool:
    return bool(
        prep.short_address_code
        or prep.google_maps_url
        or _has_structured_address(prep)
        or prep.address_line
    )


def _checkout_question(field_name: str) -> str:
    questions = {
        "customer_first_name": "ممتاز، ما اسمك الأول لإكمال الطلب؟",
        "customer_last_name": "وما اسم العائلة كما يظهر في عنوان التسليم؟",
        "city": "ما المدينة التي سيصلها الطلب؟",
        "address_location": (
            "أرسل الرمز الوطني المختصر للعقار، أو أرسل رابط موقعك من Google Maps "
            "وسأجهز بيانات الطلب."
        ),
    }
    return questions.get(field_name, "أرسل لي التفاصيل الناقصة لإكمال الطلب.")


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
        return f"الرمز المختصر: {prep.short_address_code}"
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
