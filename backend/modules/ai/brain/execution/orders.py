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
from typing import Any, Dict, List

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
    expand_maps_url,
    extract_address_signals,
    resolve_coordinates,
    resolve_short_address,
    spl_resolution_available,
)


class DraftOrderHandler:
    """Handles ACTION_PROPOSE_DRAFT_ORDER."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime

        # ── Product source resolution (forced_product wins) ───────────────────
        # When the engine routes from a numeric / name pick, it sets
        # `forced_product` on the decision. That value MUST win over
        # `state.current_product_focus` because the focus may still hold a
        # stale product (e.g. previous بلوزة) from before the new list was
        # displayed. Without this guard, "اخترت 1 = بنطلون" silently
        # creates an order for the WRONG product.
        _forced       = decision.args.get("forced_product") or {}
        _arg_product  = decision.args.get("product") or {}
        _focus        = ctx.state.current_product_focus or {}
        _source       = decision.args.get("source", "")

        if _forced:
            product_info = _forced
            _resolved_source = f"forced_product (source={_source!r})"
            # Update the canonical focus IMMEDIATELY so any downstream
            # code that reads ctx.state inside this handler sees the
            # corrected product, not the stale one.
            try:
                ctx.state.current_product_focus = dict(_forced)
            except Exception:
                pass
        elif _arg_product:
            product_info = _arg_product
            _resolved_source = "decision.args[product]"
        else:
            product_info = _focus
            _resolved_source = "state.current_product_focus (FALLBACK)"

        # ── Entry log: proves the handler was reached + which source won ──────
        logger.info(
            "[ORDER FLOW] DraftOrderHandler.handle() entered | tenant=%s "
            "resolved_from=%s product=%r external_id=%s "
            "forced_title=%r arg_title=%r focus_title=%r decision_source=%s",
            ctx.tenant_id,
            _resolved_source,
            (product_info or {}).get("title"),
            (product_info or {}).get("external_id"),
            (_forced or {}).get("title"),
            (_arg_product or {}).get("title"),
            (_focus or {}).get("title"),
            _source or "(none)",
        )

        # Mismatch alarm: if forced_product disagrees with focus, scream
        # loudly so any future regression is visible from a single grep.
        if _forced and _focus and (_forced.get("title") != _focus.get("title")):
            logger.warning(
                "[ORDER FLOW] forced_product overrode stale focus | "
                "forced=%r (external_id=%s) was_focus=%r (external_id=%s) source=%s",
                _forced.get("title"), _forced.get("external_id"),
                _focus.get("title"), _focus.get("external_id"), _source,
            )

        if not product_info:
            logger.error(
                "[ORDER FLOW] no product_info — no_product_focus | tenant=%s "
                "decision_args=%s current_product_focus=%s",
                ctx.tenant_id,
                list(decision.args.keys()),
                bool(ctx.state.current_product_focus),
            )
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

        # CRITICAL: ONLY use external_id (the Salla / store-platform product
        # identifier). NEVER fall back to `id` — that is the internal Nahla
        # DB primary key and Salla has no concept of it. Sending Nahla's
        # `id` to Salla yields a 422 with bogus error messages because the
        # product simply does not exist on Salla under that ID.
        current_product_id = str(product_info.get("external_id") or "").strip()
        previous_product_id = str(getattr(prev_prep, "product_id", "") or "")
        product_changed = bool(current_product_id and previous_product_id and current_product_id != previous_product_id)
        previous_failed = bool(getattr(prev_prep, "last_order_failed", False))

        if product_changed:
            logger.info(
                "[DraftOrderHandler] Product changed — resetting address + options | "
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
            prep.product_options_meta = []
            prep.product_options = {}
            prep.product_has_required_options = False
            # Reset the load-cache so we re-fetch options for the new
            # product. Also clear any previous unsyncable verdict — that
            # was about the OLD product.
            prep.product_options_loaded = False
            prep.product_unsyncable = False

        # Track which product this prep belongs to
        prep.product_id = current_product_id

        # is_first_ask: True when no customer data exists yet (very first data-collection turn)
        _is_first_ask = not bool(prep.customer_first_name or prep.city or prep.short_address_code)

        # Snapshot pre-merge state to detect if this turn brought new
        # address signals (used both for the "address captured during
        # options phase" log and to avoid clearing previously captured
        # address fields after the customer has moved on to options).
        _had_address_before = bool(
            prep.short_address_code or prep.google_maps_url or prep.address_line
        )
        _had_city_before = bool(prep.city)
        _had_prep_before = bool(
            prep.customer_first_name
            or prep.city
            or prep.short_address_code
            or prep.google_maps_url
            or prep.product_options
        )

        # ── EARLY product validation ──────────────────────────────────────
        # CRITICAL: validate the external_id and load product options BEFORE
        # checking `missing` fields. The previous ordering put these checks
        # AFTER the `if missing: return` early-exit, so for the first 2
        # turns (city, address) we never hit Salla at all. On Turn 3 (the
        # first time all fields are filled) we'd call get_product() for the
        # VERY FIRST TIME — and a single transient Salla hiccup would mark
        # the product as unsyncable, producing the "غير متاح" message after
        # the customer had already given their city and address code.
        #
        # By moving it here, Turn 1 (product pick) validates immediately:
        #   • If the product is bad → customer hears it NOW, not 3 turns later.
        #   • If it's good → product_options_loaded=True on Turn 1, so Turns
        #     2 and 3 skip the Salla call entirely (via the loaded guard).
        external_id = str(product_info.get("external_id") or "").strip()
        if not external_id:
            logger.error(
                "[ORDER FLOW] invalid product — no external_id (Salla product id missing) | "
                "tenant=%s nahla_db_id=%s title=%r product_info=%s",
                ctx.tenant_id,
                product_info.get("id"),
                product_info.get("title"),
                product_info,
            )
            return ActionResult(
                success=True,
                data={
                    "product_unsyncable": True,
                    "message": "product_missing_external_id",
                    "product": product_info,
                    "order_prep": prep.to_dict(),
                },
            )

        logger.info(
            "[ORDER FLOW] product validated | nahla_db_id=%s salla_product_id=%s name=%r "
            "options_loaded=%s unsyncable=%s turn_product_changed=%s",
            product_info.get("id"), external_id, product_info.get("title"),
            prep.product_options_loaded, prep.product_unsyncable, product_changed,
        )

        await _ensure_product_options_loaded(prep, ctx, external_id)

        if prep.product_unsyncable:
            logger.error(
                "[ORDER FLOW] aborting order — product not found on Salla | "
                "tenant=%s salla_product_id=%s name=%r",
                ctx.tenant_id, external_id, product_info.get("title"),
            )
            return ActionResult(
                success=True,
                data={
                    "product_unsyncable": True,
                    "message": "product_not_on_store",
                    "product": product_info,
                    "order_prep": prep.to_dict(),
                },
            )

        # ── From here on, the product is confirmed to exist on Salla. ─────

        _seed_checkout_state(prep, ctx)

        # ── Consume any address signals stashed BEFORE a product was
        # picked. The customer e.g. typed "TAPA7401" while still
        # browsing; we saved it on `state.pending_*` and now that they
        # picked a product we must merge it into prep so we never re-ask.
        _consumed_pending_address = False
        _pending_short = (getattr(ctx.state, "pending_short_address_code", "") or "").strip()
        _pending_maps  = (getattr(ctx.state, "pending_google_maps_url", "") or "").strip()
        _pending_city  = (getattr(ctx.state, "pending_city", "") or "").strip()
        if _pending_short and not prep.short_address_code:
            prep.short_address_code = _pending_short
            _consumed_pending_address = True
        if _pending_maps and not prep.google_maps_url:
            prep.google_maps_url = _pending_maps
            _consumed_pending_address = True
        if _pending_city and not prep.city:
            prep.city = _pending_city
            _consumed_pending_address = True
        if _consumed_pending_address:
            logger.info(
                "[ORDER FLOW] consumed pending address (captured before product pick) | "
                "tenant=%s short_code=%r maps=%s city=%r",
                ctx.tenant_id,
                prep.short_address_code,
                bool(prep.google_maps_url),
                prep.city,
            )

        _merge_message_details(prep, ctx.intent.slots, ctx.message)
        await _resolve_checkout_address(prep)

        if _had_prep_before:
            logger.info(
                "[ORDER FLOW] continuing flow with preserved data | tenant=%s "
                "name=%r city=%r short_code=%r options_picked=%d",
                ctx.tenant_id,
                bool(prep.customer_first_name),
                prep.city,
                prep.short_address_code,
                len(prep.product_options or {}),
            )

        # Country-aware address rules
        is_sa = _is_saudi_customer(ctx.customer_phone)
        missing = _missing_checkout_fields(prep, is_sa=is_sa)
        prep.missing_fields = missing

        # ── Verbose checkpoint: show exactly what's collected vs. missing ──────
        logger.info(
            "[ORDER FLOW] checkout fields status | tenant=%s product=%r "
            "first_name=%r last_name=%r city=%r "
            "short_code=%r maps_url=%s lat_lng=%s "
            "missing=%s is_sa=%s",
            ctx.tenant_id, product_info.get("title"),
            bool(prep.customer_first_name), bool(prep.customer_last_name),
            prep.city or None,
            prep.short_address_code or None, bool(prep.google_maps_url),
            bool(prep.latitude and prep.longitude),
            missing, is_sa,
        )

        if missing:
            logger.info(
                "[ORDER FLOW] BLOCKED → needs_collection | tenant=%s product=%r "
                "missing=%s next_question=%r",
                ctx.tenant_id, product_info.get("title"),
                missing,
                _checkout_question(missing[0], is_sa=is_sa),
            )
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

        _merge_message_options(prep, ctx.message)

        # ── Number-by-stage interpretation (quantity) ────────────────────────
        # When the customer sends a bare number ("2" / "3") AND the product
        # is already selected AND no required options are pending, treat it
        # as a quantity update. The same digits would have been interpreted
        # as a list-pick (no product yet) or option-pick (options pending)
        # in the earlier stages — this branch only fires when both prior
        # interpretations are no longer ambiguous.
        _msg_clean = (ctx.message or "").strip()
        if (
            _msg_clean.isdigit()
            and 1 <= len(_msg_clean) <= 2
            and not _missing_product_options(prep)
        ):
            try:
                _qty_from_msg = int(_msg_clean)
            except ValueError:
                _qty_from_msg = 0
            if _qty_from_msg >= 1 and _qty_from_msg != int(prep.quantity or 1):
                prep.quantity = _qty_from_msg
                logger.info(
                    "[ORDER FLOW] number interpreted as quantity | tenant=%s "
                    "product=%s quantity=%d",
                    ctx.tenant_id, external_id, _qty_from_msg,
                )

        # If the customer dropped a short_code / Maps URL / city while
        # we were still collecting product options, log it explicitly so
        # we can verify in Railway that data preservation works.
        _has_address_now = bool(
            prep.short_address_code or prep.google_maps_url or prep.address_line
        )
        if (not _had_address_before) and _has_address_now:
            logger.info(
                "[ORDER FLOW] address captured during options phase | tenant=%s "
                "short_code=%r maps=%s city=%r options_pending=%s",
                ctx.tenant_id,
                prep.short_address_code,
                bool(prep.google_maps_url),
                prep.city,
                [g.get("name") for g in _missing_product_options(prep)],
            )
        elif (not _had_city_before) and prep.city and _missing_product_options(prep):
            logger.info(
                "[ORDER FLOW] address captured during options phase | tenant=%s "
                "city=%r (newly added)",
                ctx.tenant_id, prep.city,
            )

        _missing_options = _missing_product_options(prep)
        if _missing_options:
            _next_group = _missing_options[0]
            logger.info(
                "[ORDER FLOW] product requires options | tenant=%s product=%s "
                "missing=%s selected=%s",
                ctx.tenant_id, external_id,
                [g["name"] for g in _missing_options],
                list(prep.product_options.keys()),
            )
            logger.info(
                "[ORDER FLOW] missing product options | tenant=%s pending=%s values=%s",
                ctx.tenant_id, _next_group["name"],
                [v["name"] for v in _next_group.get("values") or []],
            )
            return ActionResult(
                success=True,
                data={
                    "product": product_info,
                    "needs_options": True,
                    "missing_option_groups": _missing_options,
                    "selected_options": prep.product_options,
                    "order_prep": prep.to_dict(),
                },
            )
        if prep.product_has_required_options:
            _final_selection = {
                k: v.get("value_name") for k, v in (prep.product_options or {}).items()
            }
            logger.info(
                "[ORDER FLOW] product options selected | tenant=%s product=%s selection=%s",
                ctx.tenant_id, external_id, _final_selection,
            )
            # "all options collected" — emitted on the turn that completes
            # the option set so we can trace the boundary between option-
            # collection turns and order creation.
            _prev_selection_count = len(getattr(prev_prep, "product_options", None) or {})
            if len(prep.product_options or {}) > _prev_selection_count:
                logger.info(
                    "[ORDER FLOW] all options collected | tenant=%s product=%s count=%d selection=%s",
                    ctx.tenant_id, external_id,
                    len(prep.product_options or {}), _final_selection,
                )

        # Log phone resolution — phone is always taken from the WhatsApp conversation,
        # never asked from the customer.
        _resolved_phone = ctx.customer_phone or ""
        logger.info(
            "[ORDER FLOW] phone resolved from conversation | phone=%s tenant=%s",
            _resolved_phone, ctx.tenant_id,
        )

        # ── Shipping resolution ───────────────────────────────────────────────────
        # Resolve shipping company ID automatically — never ask the customer.
        # Cache the result in prep so repeated turns don't re-fetch.
        if not prep.shipping_company_id:
            logger.info(
                "[ORDER FLOW] resolving shipping method | tenant=%s city=%r",
                ctx.tenant_id, prep.city,
            )
            try:
                from store_integration.order_service import (  # noqa: PLC0415
                    get_default_shipping_company_id as _get_sid,
                )
                _sid = await _get_sid(ctx.tenant_id, prep.city)
                if _sid:
                    prep.shipping_company_id = _sid
                    logger.info(
                        "[ORDER FLOW] selected default shipping method | company_id=%s tenant=%s",
                        _sid, ctx.tenant_id,
                    )
                else:
                    logger.info(
                        "[ORDER FLOW] shipping method unavailable, proceeding without | "
                        "tenant=%s city=%r",
                        ctx.tenant_id, prep.city,
                    )
            except Exception as _exc:
                logger.warning(
                    "[ORDER FLOW] shipping resolution error (non-blocking) | tenant=%s err=%s",
                    ctx.tenant_id, _exc,
                )
        else:
            logger.info(
                "[ORDER FLOW] using cached shipping method | company_id=%s tenant=%s",
                prep.shipping_company_id, ctx.tenant_id,
            )

        logger.info(
            "[ORDER FLOW] All data collected → creating order | tenant=%s "
            "product=%s external_id=%s name=%r phone=%s city=%r "
            "short_code=%r has_maps=%s quantity=%d shipping_id=%s previous_failed=%s",
            ctx.tenant_id,
            product_info.get("title", "?"),
            external_id,
            (prep.customer_first_name + " " + prep.customer_last_name).strip(),
            _resolved_phone[-4:] if _resolved_phone else "????",
            prep.city,
            prep.short_address_code,
            bool(prep.google_maps_url),
            max(int(prep.quantity or 1), 1),
            prep.shipping_company_id,
            previous_failed,
        )

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        _options_payload = _resolve_options_payload(prep)

        # ── FINAL CHECK — log every condition before posting to Salla ─────────
        # If any line here shows False/missing, that is why the order failed.
        _has_city       = bool(prep.city and prep.city.strip())
        _has_short_code = bool(prep.short_address_code and prep.short_address_code.strip())
        _has_maps       = bool(prep.google_maps_url)
        _has_lat_lng    = bool(prep.latitude and prep.longitude)
        _has_address    = _has_short_code or _has_maps or _has_lat_lng
        _has_name       = bool(prep.customer_first_name)
        _has_options    = bool(_options_payload)
        _needs_opts     = bool(prep.product_has_required_options)
        _can_checkout   = product_info.get("can_checkout", product_info.get("orderable", True))
        logger.error(
            "[ORDER FLOW] FINAL CHECK | tenant=%s product_id=%s external_id=%s "
            "has_name=%s has_city=%s has_short_code=%s has_maps=%s has_lat_lng=%s "
            "has_address=%s has_options=%s needs_options=%s can_checkout=%s "
            "quantity=%s shipping_id=%s previous_failed=%s",
            ctx.tenant_id, product_info.get("id"), external_id,
            _has_name, _has_city, _has_short_code, _has_maps, _has_lat_lng,
            _has_address, _has_options, _needs_opts, _can_checkout,
            max(int(prep.quantity or 1), 1), prep.shipping_company_id, previous_failed,
        )

        # ── Mandatory diagnostic log: ALWAYS emit the final order options
        # so we can compare what we *think* we collected vs. what Salla
        # actually receives. Even an empty list is informative — it tells
        # us at a glance whether the option pipeline ran end-to-end.
        logger.info(
            "[ORDER FLOW] final order options | tenant=%s product_id=%s "
            "has_required=%s state_options=%s payload=%s",
            ctx.tenant_id,
            external_id,
            prep.product_has_required_options,
            {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
            _options_payload,
        )

        # ── Hard guard: never POST /orders if the product has required
        # options and the resolved payload is empty. This protects against
        # any path that might bypass the earlier `_missing_product_options`
        # check (e.g. stale state where options_meta was loaded but the
        # selection map got cleared, or an "أنشئ الطلب" message arriving
        # before the customer has actually picked size/colour).
        if prep.product_has_required_options and not _options_payload:
            logger.error(
                "[ORDER FLOW] blocking create_order: required options missing in final payload | "
                "tenant=%s product=%s required_groups=%s selected=%s",
                ctx.tenant_id, external_id,
                [g.get("name") for g in (prep.product_options_meta or []) if g.get("required")],
                list((prep.product_options or {}).keys()),
            )
            _missing_now = _missing_product_options(prep) or list(prep.product_options_meta or [])
            return ActionResult(
                success=True,
                data={
                    "product": product_info,
                    "needs_options": True,
                    "missing_option_groups": _missing_now,
                    "selected_options": prep.product_options,
                    "order_prep": prep.to_dict(),
                },
            )

        if _options_payload:
            logger.info(
                "[ORDER FLOW] creating order with options | tenant=%s product=%s options=%s",
                ctx.tenant_id, external_id, _options_payload,
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
                "shipping_company_id": prep.shipping_company_id,
                "options": _options_payload,
            },
        )
        order = runtime_result.payload.get("order")

        if order:
            order_id = order.get("id") or ""
            # Extract payment URL — check every field the adapter may populate.
            checkout_url = (
                order.get("payment_link")
                or order.get("payment_url")
                or order.get("checkout_url")
                or (order.get("urls") or {}).get("payment")
                or (order.get("urls") or {}).get("checkout")
                or ""
            )
            logger.info(
                "[ORDER FLOW] order created | order_id=%s tenant=%s",
                order_id, ctx.tenant_id,
            )
            if checkout_url:
                logger.info(
                    "[ORDER FLOW] payment url extracted | %s tenant=%s",
                    checkout_url, ctx.tenant_id,
                )
            else:
                logger.warning(
                    "[ORDER FLOW] order created but payment url missing | order_id=%s tenant=%s",
                    order_id, ctx.tenant_id,
                )
            return ActionResult(
                success=True,
                data={
                    "order_id":    order_id,
                    "reference":   order.get("reference_id") or order_id,
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

        # ── Salla payload validation failed BEFORE POST ───────────────────────
        # The adapter pre-flight (validate_salla_order_payload) found that one
        # or more fields Salla truly requires were missing.  We turn those
        # canonical names into checkout slots so the conversation layer asks
        # the customer for them — never fabricating a "تم إنشاء الطلب" line.
        _missing_payload_fields = list(
            (runtime_result.payload or {}).get("missing_fields") or []
        )
        if error_msg == "salla_payload_invalid" or _missing_payload_fields:
            # Map the validator's canonical names back to OrderPreparation
            # slots used by the conversation. Fields the customer cannot
            # provide (product_id) are reported separately as a hard catalog
            # error.
            _slot_map = {
                "customer_first_name": "customer_first_name",
                "customer_last_name":  "customer_last_name",
                "customer_phone":      "customer_phone",
                "city":                "city",
                "address":             "address_location",
                "payment_method":      "payment_method",
            }
            _slots_to_collect = [
                _slot_map[m] for m in _missing_payload_fields if m in _slot_map
            ]
            _hard_errors = [m for m in _missing_payload_fields if m not in _slot_map]

            logger.error(
                "[ORDER FLOW] Salla validation BLOCKED order | tenant=%s product=%s "
                "missing=%s slots_to_collect=%s hard_errors=%s",
                ctx.tenant_id, external_id,
                _missing_payload_fields, _slots_to_collect, _hard_errors,
            )

            if _hard_errors:
                # Catalog-level: cannot proceed — tell the customer the product
                # is unavailable and break the loop instead of asking for slots
                # they couldn't fill anyway.
                return ActionResult(
                    success=True,
                    data={
                        "product_unsyncable": True,
                        "message":  "product_payload_invalid",
                        "product":  product_info,
                        "missing":  _hard_errors,
                        "order_prep": prep.to_dict(),
                    },
                )

            # Mark which slots the conversation must collect next, then ask.
            # The conversation layer reads `prep.missing_fields` and asks for
            # the first one in Arabic.
            prep.missing_fields = _slots_to_collect or list(prep.missing_fields or [])
            return ActionResult(
                success=True,
                data={
                    "product":         product_info,
                    "needs_collection": True,
                    "missing_fields":  prep.missing_fields,
                    "is_first_ask":    not (
                        prep.customer_first_name or prep.city or prep.short_address_code
                    ),
                    "question":        _ask_for_missing_field(prep.missing_fields),
                    "external_create_failed": True,
                    "external_failure_reason": "salla_payload_invalid",
                    "order_prep":      prep.to_dict(),
                },
            )

        # ── Salla rejected the product because options are missing ────────────
        # Either our adapter pre-flight caught it, or Salla itself returned
        # 422 ("خيارات المنتج مطلوبة"). Either way: we now KNOW this product
        # needs options. Persist the fact, force-reload metadata, and
        # re-prompt the customer instead of looping the generic retry.
        _options_missing_signal = (
            "required_product_options_missing" in error_msg
            or "options" in error_msg.lower()
            or "خيارات المنتج" in error_msg
        )
        if _options_missing_signal:
            prep.product_has_required_options = True
            # Drop cached metadata so the next turn re-fetches from
            # Salla via the dedicated /products/{id}/options endpoint.
            prep.product_options_meta = []
            prep.product_options = {}
            prep.product_options_loaded = False
            logger.error(
                "[ORDER FLOW] options required by Salla — reloading metadata | "
                "tenant=%s product=%s",
                ctx.tenant_id, external_id,
            )
            await _ensure_product_options_loaded(prep, ctx, external_id)
            _missing_now = _missing_product_options(prep) or list(prep.product_options_meta or [])

            # Guard: if Salla says options are required but we STILL can't load
            # them (get_product keeps returning None), returning needs_options
            # with an empty list causes the composer to say "تمام، سأجهز طلب"
            # and then loop forever. Fall through to the retry/escalate path
            # instead so a human agent can handle it.
            if not _missing_now and not prep.product_options_meta:
                logger.warning(
                    "[ORDER FLOW] options required by Salla but metadata unavailable "
                    "after reload — falling to salla_retry/escalate | "
                    "tenant=%s product=%s",
                    ctx.tenant_id, external_id,
                )
                prep.salla_failure_count = (prep.salla_failure_count or 0) + 1
                # Fall through to retry/escalate handling below.
            else:
                return ActionResult(
                    success=True,
                    data={
                        "product": product_info,
                        "needs_options": True,
                        "missing_option_groups": _missing_now,
                        "selected_options": prep.product_options,
                        "order_prep": prep.to_dict(),
                    },
                )

        if prep.customer_first_name and prep.city:
            has_address = bool(prep.short_address_code or prep.google_maps_url or prep.latitude)

            # ── CRITICAL: NEVER clear address fields just because Salla failed ──
            # The customer already provided their address. The failure is Salla-side
            # (bad payload, shipping issue, etc.) — not a missing-address problem.
            # Clearing the address and re-asking is a broken UX that loops forever.
            prep.last_order_failed = True
            prep.salla_failure_count = (prep.salla_failure_count or 0) + 1
            prep.missing_fields = []

            logger.error(
                "[ORDER FLOW] Order creation FAILED #%d | tenant=%s "
                "short_code=%r google_maps=%s has_address=%s error=%r",
                prep.salla_failure_count, ctx.tenant_id,
                prep.short_address_code, bool(prep.google_maps_url),
                has_address, error_msg,
            )

            if has_address and prep.salla_failure_count <= 1:
                # First failure with address → silent retry message.
                # Customer should send any message to trigger another attempt.
                logger.info(
                    "[ORDER FLOW] retry attempt | tenant=%s product=%s attempt=%d "
                    "short_code=%r",
                    ctx.tenant_id, external_id,
                    prep.salla_failure_count, prep.short_address_code,
                )
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

            if has_address and prep.salla_failure_count >= 2:
                # Second+ failure — escalate to human without clearing any data.
                # Do NOT re-ask for address: the data is already there.
                return ActionResult(
                    success=True,
                    data={
                        "product": product_info,
                        "salla_escalate": True,
                        "salla_failure_count": prep.salla_failure_count,
                        "order_prep": prep.to_dict(),
                        "order_creation_error": error_msg,
                    },
                )

            # No address was provided at all — ask for it once
            if not has_address:
                prep.missing_fields = ["address_location"]
                return ActionResult(
                    success=True,
                    data={
                        "product": product_info,
                        "needs_collection": True,
                        "missing_fields": ["address_location"],
                        "question": (
                            "أرسل لي الرمز الوطني المختصر للعنوان (مثال: RIYD1234) "
                            "أو رابط موقعك من خرائط جوجل."
                        ),
                        "is_first_ask": not previous_failed,
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


_ORDER_STATUS_AR: dict = {
    "pending":           "قيد الانتظار",
    "in_progress":       "قيد التنفيذ",
    "under_review":      "تحت المراجعة",
    "processing":        "جاري المعالجة",
    "confirmed":         "مؤكّد",
    "shipped":           "تم الشحن",
    "on_the_way":        "في الطريق",
    "out_for_delivery":  "خارج للتوصيل",
    "delivered":         "تم التسليم",
    "completed":         "مكتمل",
    "cancelled":         "ملغي",
    "refunded":          "مُسترجع",
    "returned":          "مُرتجع",
    "failed":            "فشل",
    "cod":               "دفع عند الاستلام",
}


class TrackOrderHandler:
    """Handles ACTION_TRACK_ORDER.

    Improvements (Phase roadmap):
    - Extracts order_number from intent slots so the customer can ask about
      a specific order ("ما حال طلبي رقم 12345") instead of always getting
      the latest one.
    - Returns richer data: Arabic status label + up to 3 item titles so the
      Composer can render a meaningful status card.
    """

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime  # noqa: PLC0415

        # Pull order_number from intent slots or decision args
        order_number = (
            str(decision.args.get("order_number") or "").strip()
            or str(ctx.intent.slots.get("order_id") or "").strip()
            or str(ctx.intent.slots.get("order_number") or "").strip()
        )

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        runtime_result = await runtime.execute("track_order", {"order_number": order_number})
        latest: dict = runtime_result.payload.get("order") if runtime_result.ok else {}

        if not latest:
            return ActionResult(
                success=False,
                error="no_orders",
                data={"message": "no_orders_found"},
            )

        raw_status = str(latest.get("status") or "").lower().replace(" ", "_")
        status_ar = _ORDER_STATUS_AR.get(raw_status) or latest.get("status") or "—"

        # Summarise items (max 3 titles) for the response template
        items = latest.get("items") or []
        item_titles = [
            str(it.get("name") or it.get("title") or it.get("product_name") or "")
            for it in items[:3]
            if it.get("name") or it.get("title") or it.get("product_name")
        ]

        return ActionResult(
            success=True,
            data={
                "order_id":        latest.get("id"),
                "reference":       latest.get("reference_id") or latest.get("id"),
                "status":          latest.get("status"),
                "status_label_ar": status_ar,
                "total":           latest.get("total"),
                "currency":        latest.get("currency", "SAR"),
                "item_titles":     item_titles,
                "matched_by_ref":  runtime_result.payload.get("matched_by_ref", False),
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
    # Step 0: If we have a shortened Google Maps URL but no coordinates yet,
    # follow the redirect to recover the full URL and extract lat/lng from it.
    # This handles the most common SA pattern: customer shares maps.app.goo.gl/xxx.
    if prep.google_maps_url and prep.latitude is None:
        expanded = await expand_maps_url(prep.google_maps_url)
        if expanded and expanded != prep.google_maps_url:
            from services.address_resolution import _extract_coords  # noqa: PLC0415
            lat, lng = _extract_coords(expanded)
            if lat is not None and lng is not None:
                prep.latitude  = lat
                prep.longitude = lng
                logger.info(
                    "[ORDER FLOW] coords extracted from expanded maps URL | "
                    "lat=%.6f lng=%.6f tenant=%s",
                    lat, lng, getattr(prep, "product_id", "?"),
                )

    # Step 1: Resolve national short address code via SPL API.
    if prep.short_address_code and not _has_structured_address(prep):
        resolved = await resolve_short_address(prep.short_address_code, city=prep.city)
        _merge_resolved_address(prep, resolved)

    # Step 2: Reverse-geocode coordinates (from direct user input OR from
    # the expanded maps URL above) via SPL API.
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
    """Human-readable address text passed to Salla as the order address.

    NEVER return the bare national short code (e.g. TAPA7401) — Salla
    rejects alphanumeric codes as street values. Always wrap the code or
    the maps URL with a readable Arabic prefix and the city when known.
    """
    if prep.address_line:
        return prep.address_line
    if prep.street:
        suffix = f" - {prep.district}" if prep.district else ""
        return f"{prep.street}{suffix}".strip()
    city = (prep.city or "").strip()
    if prep.short_address_code:
        code = prep.short_address_code.strip().upper()
        if city:
            return f"{city} - الرمز الوطني {code}"
        return f"العنوان عبر الرمز الوطني {code}"
    if prep.google_maps_url:
        if city:
            return f"{city} - الموقع عبر خرائط Google"
        return "الموقع عبر خرائط Google"
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


_MISSING_FIELD_PROMPTS_AR: Dict[str, str] = {
    "customer_first_name": "ما اسمك الأول؟",
    "customer_last_name":  "ما اسم العائلة؟",
    "customer_phone":      "أرسل رقم جوال للتواصل عند التوصيل (مثال: 0555xxxxxx).",
    "city":                "في أي مدينة سنوصل لك الطلب؟",
    "address_location":    (
        "أرسل عنوان التوصيل: يمكن الرمز الوطني (مثل TAPA7401) "
        "أو رابط الموقع من خرائط Google."
    ),
    "payment_method":      (
        "كيف تفضّل الدفع؟ \n• الدفع عند الاستلام\n• تحويل بنكي\n"
        "اكتب اختيارك."
    ),
}


def _ask_for_missing_field(missing: List[str]) -> str:
    """Return the Arabic prompt for the FIRST missing field.  Designed for
    the single-question UX used by the WhatsApp order flow — never asks for
    more than one slot at a time."""
    for slot in (missing or []):
        prompt = _MISSING_FIELD_PROMPTS_AR.get(slot)
        if prompt:
            return prompt
    return "هل يمكنك تأكيد بيانات التوصيل وطريقة الدفع؟"


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


# ─────────────────────────────────────────────────────────────────────────────
# Product options (variants) helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_product_options_loaded(
    prep: OrderPreparationState,
    ctx: BrainContext,
    external_id: str,
) -> None:
    """Fetch the product's option groups from the store once and cache them.

    Salla products with size/color/etc. variants expose `options` in their
    detail payload; if this prep already has cached metadata for the same
    product we skip the network call.
    """
    # CRITICAL: key off the explicit `product_options_loaded` boolean —
    # NOT the truthiness of `product_options_meta`. A simple product
    # legitimately has an empty options list, and the old check
    # (`if prep.product_options_meta`) treated `[]` as "never loaded",
    # so we re-hit Salla on EVERY single turn. After 2-3 calls one of
    # them returns transiently empty (rate-limit, gateway hiccup, 200
    # with `data: null`) and we wrongly flip `product_unsyncable=True`
    # — which produced the "هذا المنتج غير متاح" message after the
    # customer had already given city + short address code.
    if prep.product_options_loaded:
        return
    if not external_id:
        return
    try:
        from store_integration.registry import get_adapter  # noqa: PLC0415
        adapter = get_adapter(ctx.tenant_id)
        if not adapter:
            return
        product = await adapter.get_product(external_id)
        if not product:
            # Salla returned nothing for this id.  Two possible causes:
            #   1. Transient API issue (rate-limit, 200+null body, gateway hiccup)
            #   2. Product was deleted from Salla after the last catalog sync.
            #
            # We CANNOT distinguish these two cases from a single None response,
            # so we MUST NOT permanently block the order.  Instead we skip the
            # options pre-check and let the actual POST /orders call surface the
            # real error.  If the product truly doesn't exist, Salla returns a
            # 422/404 which the retry/escalate flow handles gracefully.  If it
            # was a transient hiccup, the order succeeds — much better UX.
            #
            # belt-and-braces: the early-return above already handles the case
            # where product_options_loaded is True (product was good before).
            if prep.product_options_loaded:
                logger.warning(
                    "[ORDER FLOW] transient empty get_product result — "
                    "ignoring (product was loaded successfully before) | "
                    "tenant=%s product_id=%s",
                    ctx.tenant_id, external_id,
                )
                return
            logger.warning(
                "[ORDER FLOW] get_product(%s) returned None — skipping options "
                "pre-check and attempting order creation; Salla will surface "
                "the real error if the product no longer exists | tenant=%s",
                external_id, ctx.tenant_id,
            )
            # Mark as loaded so subsequent turns don't keep hammering Salla.
            prep.product_options_loaded = True
            prep.product_unsyncable = False
            return
        prep.product_unsyncable = False
        prep.product_options_loaded = True
        opts = list(getattr(product, "options", None) or [])
        prep.product_options_meta = opts
        # ── Defensive default: any product with option groups (size,
        # colour, …) is treated as REQUIRING a customer choice. Salla
        # is unreliable about the per-group `required` flag — we have
        # seen 422 ("خيارات المنتج مطلوبة") for products whose option
        # objects had `required: false`. Sending an option that Salla
        # deems optional is harmless; skipping a required one blocks
        # the order. So: presence of option groups ⇒ ask the
        # customer.
        groups_with_values = [o for o in opts if o.get("values")]
        prep.product_has_required_options = bool(groups_with_values)
        logger.info(
            "[ORDER FLOW] product options loaded | tenant=%s product_id=%s "
            "groups=%d has_required=%s groups_meta=%s",
            ctx.tenant_id,
            external_id,
            len(opts),
            prep.product_has_required_options,
            [
                {"id": o.get("id"), "name": o.get("name"),
                 "values_count": len(o.get("values") or [])}
                for o in opts
            ],
        )
        if prep.product_has_required_options:
            logger.info(
                "[ORDER FLOW] product requires options | tenant=%s product_id=%s "
                "groups=%s",
                ctx.tenant_id,
                external_id,
                [o.get("name") for o in groups_with_values],
            )
    except Exception as exc:
        logger.warning(
            "[ORDER FLOW] product options fetch failed (non-blocking) | "
            "tenant=%s product=%s err=%s",
            ctx.tenant_id, external_id, exc,
        )


def _merge_message_options(prep: OrderPreparationState, message: str) -> int:
    """Match values mentioned in the customer's message against cached options.

    Rule-based, runs against ALL groups (multi-option in a single message
    such as "M أسود" or "2 1") with override support:

      1. Direct value-name substring match (case-insensitive + Arabic
         normalised) across every required group. If a different value
         from the SAME group is already selected, this REPLACES it and
         logs `[ORDER FLOW] option updated` so the customer can change
         their mind ("M أسود" → "أبيض" updates only the colour).
      2. Multi-token numeric pick — splits the message on whitespace,
         so "2 1" assigns 2 to the first pending group and 1 to the
         second. Numeric picks are positional/ambiguous, so they only
         operate on groups that are still UNSELECTED to avoid
         accidentally overriding a deliberate text choice.

    Returns the number of groups newly selected (or updated) on this
    turn so callers can emit `[ORDER FLOW] multi-option parsed` when
    ≥2 captured.
    """
    if not prep.product_options_meta:
        return 0
    text = (message or "").strip()
    if not text:
        return 0
    text_lower = text.lower()
    text_norm = _norm_ar(text_lower)
    captured = 0

    # ── direct value-name match across all groups (with override) ────────
    for group in prep.product_options_meta:
        gname = (group.get("name") or "").strip()
        if not gname:
            continue
        gkey = gname.lower()
        existing = (prep.product_options or {}).get(gkey)
        existing_value_id = (existing or {}).get("value_id")
        existing_value_name = (existing or {}).get("value_name") or ""
        for val in group.get("values") or []:
            vname = (val.get("name") or "").strip()
            if not vname:
                continue
            v_lower = vname.lower()
            v_norm = _norm_ar(v_lower)
            if v_lower in text_lower or (v_norm and v_norm in text_norm):
                # Same value already picked — no-op, no log spam.
                if existing and (
                    existing_value_id == val.get("id")
                    or existing_value_name.lower() == v_lower
                ):
                    break
                prep.product_options[gkey] = {
                    "option_id": group.get("id"),
                    "option_name": gname,
                    "value_id": val.get("id"),
                    "value_name": vname,
                }
                captured += 1
                if existing:
                    logger.info(
                        "[ORDER FLOW] option updated | group=%r old=%r new=%r",
                        gname, existing_value_name, vname,
                    )
                else:
                    logger.info(
                        "[ORDER FLOW] product option selected | group=%r value=%r",
                        gname, vname,
                    )
                break

    # ── multi-token numeric pick across remaining pending groups ──────────
    pending_after_text = _missing_product_options(prep)
    if pending_after_text:
        tokens = [t for t in text.split() if t.isdigit()]
        for i, group in enumerate(pending_after_text):
            if i >= len(tokens):
                break
            try:
                idx = int(tokens[i])
            except ValueError:
                continue
            values = group.get("values") or []
            if not (1 <= idx <= len(values)):
                continue
            val = values[idx - 1]
            gname = group.get("name") or ""
            gkey = gname.lower()
            if gkey in (prep.product_options or {}):
                continue
            prep.product_options[gkey] = {
                "option_id": group.get("id"),
                "option_name": gname,
                "value_id": val.get("id"),
                "value_name": val.get("name") or "",
            }
            captured += 1
            logger.info(
                "[ORDER FLOW] number interpreted as option | group=%r idx=%d value=%r",
                gname, idx, val.get("name") or "",
            )

    if captured >= 2:
        logger.info(
            "[ORDER FLOW] multi-option parsed | captured=%d selection=%s",
            captured,
            {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
        )

    return captured


def _missing_product_options(prep: OrderPreparationState) -> List[Dict[str, Any]]:
    """List the option groups that the customer hasn't picked yet.

    Salla's per-group `required` flag is unreliable — we have seen 422
    ("خيارات المنتج مطلوبة") for option groups Salla returned with
    `required: false`. Any group with values is therefore treated as
    requiring a customer pick.
    """
    out: List[Dict[str, Any]] = []
    for group in prep.product_options_meta or []:
        if not (group.get("values") or []):
            continue
        gkey = (group.get("name") or "").strip().lower()
        if not gkey:
            continue
        if gkey in (prep.product_options or {}):
            continue
        out.append(group)
    return out


def _resolve_options_payload(prep: OrderPreparationState) -> List[Dict[str, Any]]:
    """Convert prep.product_options dict into OrderItemInput.options shape."""
    payload: List[Dict[str, Any]] = []
    for sel in (prep.product_options or {}).values():
        if not isinstance(sel, dict):
            continue
        if sel.get("option_id") is None:
            continue
        payload.append({
            "option_id": sel.get("option_id"),
            "option_name": sel.get("option_name"),
            "value_id": sel.get("value_id"),
            "value_name": sel.get("value_name"),
        })
    return payload


def _norm_ar(text: str) -> str:
    """Lossy normalization for Arabic substring matching: strip diacritics
    and unify alef/ya forms so 'أبيض' matches 'ابيض'."""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in "ًٌٍَُِّْـ":
            continue
        if ch in "أإآ":
            out.append("ا")
        elif ch == "ى":
            out.append("ي")
        elif ch == "ة":
            out.append("ه")
        else:
            out.append(ch)
    return "".join(out)

