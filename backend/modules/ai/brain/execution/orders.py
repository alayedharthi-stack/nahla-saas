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
from typing import Any, Dict, List, Optional, Tuple

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
            prep.product_variants_raw = []
            prep.predicted_options = {}
            prep.prediction_source = ""
            prep.prediction_confidence = 0.0
            prep.awaiting_option_confirmation = False

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
        if decision.args.get("order_context_update"):
            _fulfillment_slots = {
                k: v
                for k, v in (decision.args or {}).items()
                if k in {
                    "google_maps_url", "location_url", "short_address_code",
                    "city", "address", "address_line", "street", "district",
                    "postal_code", "building_number", "additional_number",
                    "latitude", "longitude", "customer_first_name",
                    "customer_last_name", "customer_name", "full_name",
                } and v
            }
            if _fulfillment_slots:
                _merge_message_details(prep, _fulfillment_slots, ctx.message)
                logger.info(
                    "[ORDER CONTEXT UPDATE] tenant=%s kind=%s maps=%s short=%s city=%r",
                    ctx.tenant_id,
                    decision.args.get("fulfillment_kind"),
                    bool(prep.google_maps_url),
                    bool(prep.short_address_code),
                    prep.city,
                )
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

        # ── Prediction confirmation / rejection ─────────────────────────────
        # If we sent predicted options last turn and the customer is
        # responding, handle confirm / reject BEFORE the normal option merge.
        if prep.awaiting_option_confirmation and prep.predicted_options:
            if _is_option_confirmation(ctx.message) or _is_same_as_before(ctx.message):
                # Promote predicted options → real selected options
                for _pk, _pv in (prep.predicted_options or {}).items():
                    prep.product_options[_pk] = _pv
                logger.error(
                    "[ORDER OPTIONS PREDICT] confirmed | tenant=%s product=%s "
                    "promoted=%s source=%s confidence=%.2f",
                    ctx.tenant_id, external_id,
                    {k: v.get("value_name") for k, v in prep.predicted_options.items()},
                    prep.prediction_source, prep.prediction_confidence,
                )
                _emit_predict_metric(
                    "confirmed", ctx.tenant_id,
                    product=external_id, source=prep.prediction_source,
                )
                prep.predicted_options = {}
                prep.awaiting_option_confirmation = False
                prep.prediction_source = ""
                prep.prediction_confidence = 0.0
                # Fall through — will pass the options gate now
            elif _is_option_rejection(ctx.message):
                logger.error(
                    "[ORDER OPTIONS PREDICT] rejected | tenant=%s product=%s "
                    "clearing_predicted=%s",
                    ctx.tenant_id, external_id,
                    {k: v.get("value_name") for k, v in prep.predicted_options.items()},
                )
                _emit_predict_metric(
                    "rejected", ctx.tenant_id, product=external_id,
                )
                prep.predicted_options = {}
                prep.awaiting_option_confirmation = False
                prep.prediction_source = ""
                prep.prediction_confidence = 0.0
                # Fall through — will re-check missing options below
            else:
                # Not a clear confirm/reject — try to extract a new option
                # value from the message (customer might be typing "M" or
                # "أسود" directly).
                _merge_message_options(prep, ctx.message)
                _captured_after_prediction = {
                    k: v.get("value_name")
                    for k, v in (prep.product_options or {}).items()
                }
                logger.error(
                    "[ORDER OPTIONS PREDICT] ambiguous response — extracted "
                    "options from message | tenant=%s message=%r captured=%s",
                    ctx.tenant_id, ctx.message[:60], _captured_after_prediction,
                )
                prep.predicted_options = {}
                prep.awaiting_option_confirmation = False
                prep.prediction_source = ""
                prep.prediction_confidence = 0.0
                # Fall through with whatever was captured

        # ── Always try to capture option selections from the message first ──────
        _selected_before_merge = {
            k: v.get("value_name") for k, v in (prep.product_options or {}).items()
        }
        _options_captured_early = _merge_message_options(prep, ctx.message)
        _selected_after_merge = {
            k: v.get("value_name") for k, v in (prep.product_options or {}).items()
        }
        _still_missing_after_extract = [
            g.get("name") for g in _missing_product_options(prep)
        ]
        _all_required = [
            g.get("name") for g in (prep.product_options_meta or [])
            if g.get("values")
        ]

        if prep.product_has_required_options:
            logger.error(
                "[ORDER DEBUG] stage=after_merge_options | product_id=%s "
                "selected_before=%s selected_after=%s captured=%d "
                "required=%s missing=%s "
                "product_options_loaded=%s variants_cached=%d | tenant=%s",
                external_id,
                _selected_before_merge,
                _selected_after_merge,
                _options_captured_early,
                _all_required,
                _still_missing_after_extract,
                prep.product_options_loaded,
                len(prep.product_variants_raw or []),
                ctx.tenant_id,
            )

        # Country-aware address rules
        is_sa = _is_saudi_customer(ctx.customer_phone)
        missing = _missing_checkout_fields(prep, is_sa=is_sa)
        missing = _filter_missing_phone_if_known(missing, ctx.customer_phone)
        prep.missing_fields = missing

        # ── Verbose checkpoint: show exactly what's collected vs. missing ──────
        logger.info(
            "[ORDER FLOW] checkout fields status | tenant=%s product=%r "
            "first_name=%r last_name=%r city=%r "
            "short_code=%r maps_url=%s lat_lng=%s "
            "missing=%s is_sa=%s options_pending=%s",
            ctx.tenant_id, product_info.get("title"),
            bool(prep.customer_first_name), bool(prep.customer_last_name),
            prep.city or None,
            prep.short_address_code or None, bool(prep.google_maps_url),
            bool(prep.latitude and prep.longitude),
            missing, is_sa,
            [g.get("name") for g in _missing_product_options(prep)],
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

        # Second pass: if options were not captured in the early pass
        # (because product_options_meta was not yet loaded), try again now
        # that _ensure_product_options_loaded has run.
        if not _options_captured_early and prep.product_options_meta:
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
            # ── Try prediction BEFORE asking the customer ─────────────────
            if not prep.awaiting_option_confirmation:
                try:
                    _predicted = await predict_missing_options(
                        prep, ctx, external_id, _missing_options,
                    )
                except Exception as _pred_exc:
                    logger.debug(
                        "[ORDER OPTIONS PREDICT] predict_missing_options raised | err=%s",
                        _pred_exc,
                    )
                    _predicted = None

                if _predicted and _predicted.get("confidence", 0) >= 0.7:
                    prep.predicted_options = _predicted["options"]
                    prep.prediction_source = _predicted["source"]
                    prep.prediction_confidence = _predicted["confidence"]
                    prep.awaiting_option_confirmation = True
                    logger.error(
                        "[ORDER OPTIONS PREDICT] proposing prediction | tenant=%s "
                        "product=%s source=%s confidence=%.2f predicted=%s",
                        ctx.tenant_id, external_id,
                        _predicted["source"], _predicted["confidence"],
                        {k: v.get("value_name") for k, v in _predicted["options"].items()},
                    )
                    _emit_predict_metric(
                        "predicted", ctx.tenant_id,
                        product=external_id, source=_predicted["source"],
                        confidence=f"{_predicted['confidence']:.2f}",
                    )
                    return ActionResult(
                        success=True,
                        data={
                            "product": product_info,
                            "needs_prediction_confirm": True,
                            "predicted_options": _predicted["options"],
                            "prediction_source": _predicted["source"],
                            "selected_options": prep.product_options,
                            "order_prep": prep.to_dict(),
                        },
                    )

            # No prediction or confidence too low — ask as before
            _emit_predict_metric(
                "fallback", ctx.tenant_id,
                product=external_id,
                missing=[g.get("name") for g in _missing_options],
            )
            _next_group = _missing_options[0]
            logger.error(
                "[ORDER OPTIONS] blocked_create_order reason=missing_options | "
                "tenant=%s product=%s missing=%s selected=%s "
                "next_group=%s values=%s",
                ctx.tenant_id, external_id,
                [g["name"] for g in _missing_options],
                {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
                _next_group["name"],
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
            logger.error(
                "[ORDER STATE] ready_to_create_order | product_id=%s "
                "selected_options=%s city=%s short_code=%s "
                "variants_cached=%d | tenant=%s",
                external_id, _final_selection,
                prep.city, prep.short_address_code,
                len(prep.product_variants_raw or []),
                ctx.tenant_id,
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

        # ── LOCAL VARIANT RESOLUTION ──────────────────────────────────────────
        # Try to resolve variant_id from cached raw variants.  If local match
        # succeeds → send variant_id only (no options in payload).  If local
        # match FAILS → fall through to create_order with options so the
        # adapter handles variant resolution remotely.  NEVER re-ask the
        # customer about options that are already filled.
        _resolved_variant_id: Optional[str] = None
        if prep.product_has_required_options:
            # On-demand fetch if raw variants weren't cached during options load
            if not prep.product_variants_raw:
                try:
                    from store_integration.registry import get_adapter as _get_adapter  # noqa: PLC0415
                    _adapter = _get_adapter(ctx.tenant_id)
                    if _adapter and hasattr(_adapter, "get_raw_variants"):
                        _fetched = await _adapter.get_raw_variants(external_id)
                        if _fetched:
                            prep.product_variants_raw = _fetched
                            logger.info(
                                "[ORDER VARIANT] on-demand raw variants fetched | "
                                "product_id=%s count=%d | tenant=%s",
                                external_id, len(_fetched), ctx.tenant_id,
                            )
                except Exception as _od_exc:
                    logger.debug(
                        "[ORDER VARIANT] on-demand variant fetch failed | err=%s",
                        _od_exc,
                    )

            if prep.product_variants_raw:
                _resolved_variant_id = _resolve_variant_locally(
                    prep, external_id, ctx.tenant_id,
                )

            if _resolved_variant_id:
                logger.error(
                    "[ORDER VARIANT] resolved_locally | product_id=%s "
                    "variant_id=%s selected_options=%s | tenant=%s",
                    external_id, _resolved_variant_id,
                    {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
                    ctx.tenant_id,
                )
            else:
                logger.error(
                    "[ORDER VARIANT] no_local_match — proceeding with remote "
                    "enrichment (options in payload) | product_id=%s "
                    "selected_options=%s variants_cached=%d | tenant=%s",
                    external_id,
                    {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
                    len(prep.product_variants_raw or []),
                    ctx.tenant_id,
                )

        logger.error(
            "[ORDER CREATE] calling_salla | tenant=%s product_id=%s "
            "variant_id=%s selected_options=%s city=%s short_code=%s "
            "shipping_id=%s quantity=%d",
            ctx.tenant_id, external_id, _resolved_variant_id,
            {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
            prep.city, prep.short_address_code,
            prep.shipping_company_id,
            max(int(prep.quantity or 1), 1),
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
            # _options_payload is empty — either because no options were selected,
            # OR because selected options have option_id=None (stale early capture).
            # Re-compute what is truly missing BEFORE deciding to re-ask.
            _missing_now = _missing_product_options(prep)
            # Fallback to full meta ONLY when NOTHING has been selected yet.
            # If the customer already picked (prep.product_options is non-empty) but
            # IDs are missing/stale, we should NOT re-ask — escalate instead.
            if not _missing_now and not prep.product_options:
                _missing_now = list(prep.product_options_meta or [])

            if _missing_now:
                logger.error(
                    "[ORDER FLOW] blocking create_order: options missing | "
                    "tenant=%s product=%s missing=%s selected=%s",
                    ctx.tenant_id, external_id,
                    [g.get("name") for g in _missing_now],
                    list((prep.product_options or {}).keys()),
                )
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
            # All options selected but payload empty (stale IDs): reload meta,
            # re-match selections to fresh IDs, validate, then fall through to
            # create_order — NO salla_retry, NO extra customer message required.

            # ── Retry guard ───────────────────────────────────────────────────────
            _stale_retry_count = prep.salla_failure_count or 0
            if _stale_retry_count >= 2:
                logger.error(
                    "[ORDER FLOW] retry aborted | reason=stale_id_re_match_limit "
                    "attempts=%d tenant=%s product=%s — escalating to customer",
                    _stale_retry_count, ctx.tenant_id, external_id,
                )
                return ActionResult(
                    success=True,
                    data={
                        "product": product_info,
                        "needs_options": True,
                        "missing_option_groups": list(prep.product_options_meta or []),
                        "selected_options": prep.product_options,
                        "order_prep": prep.to_dict(),
                    },
                )

            logger.warning(
                "[ORDER FLOW] re-match triggered | reason=stale_option_ids "
                "attempt=%d tenant=%s product=%s selected=%s",
                _stale_retry_count + 1, ctx.tenant_id, external_id,
                list((prep.product_options or {}).keys()),
            )
            _stale_prev = dict(prep.product_options or {})
            prep.product_options_loaded = False
            prep.product_options_meta = []
            await _ensure_product_options_loaded(prep, ctx, external_id)

            # Strict re-match: exact → arabic-normalized → reject
            if prep.product_options_meta and _stale_prev:
                _rematched, _conf_map = _rematch_options(
                    _stale_prev, prep.product_options_meta, external_id,
                )
                for _gk, _conf in _conf_map.items():
                    _inp = (_stale_prev.get(_gk) or {}).get("value_name", "")
                    _matched_id = (_rematched.get(_gk) or {}).get("value_id")
                    logger.info(
                        "[ORDER FLOW] re-match result | input=%r matched_id=%s "
                        "confidence=%s group=%r tenant=%s",
                        _inp, _matched_id, _conf, _gk, ctx.tenant_id,
                    )
                prep.product_options = _rematched

            # Re-resolve with fresh IDs
            _options_payload = _resolve_options_payload(prep)
            _payload_ok, _payload_errors = _validate_options_payload(_options_payload)

            if not _options_payload or not _payload_ok:
                prep.salla_failure_count = _stale_retry_count + 1
                logger.error(
                    "[ORDER FLOW] re-match failed | reason=no_valid_payload "
                    "errors=%s attempt=%d tenant=%s product=%s",
                    _payload_errors, _stale_retry_count + 1,
                    ctx.tenant_id, external_id,
                )
                return ActionResult(
                    success=True,
                    data={
                        "product": product_info,
                        "needs_options": True,
                        "missing_option_groups": list(prep.product_options_meta or []),
                        "selected_options": prep.product_options,
                        "order_prep": prep.to_dict(),
                    },
                )
            logger.info(
                "[ORDER FLOW] final payload validated | tenant=%s product=%s "
                "payload=%s",
                ctx.tenant_id, external_id, _options_payload,
            )

        # ── Validate payload before first Salla call ──────────────────────────
        if _options_payload:
            _init_ok, _init_errors = _validate_options_payload(_options_payload)
            if _init_ok:
                logger.info(
                    "[ORDER FLOW] initial payload ready | tenant=%s product=%s "
                    "options_count=%d options=%s",
                    ctx.tenant_id, external_id, len(_options_payload), _options_payload,
                )
            else:
                logger.warning(
                    "[ORDER FLOW] initial payload has invalid IDs | tenant=%s "
                    "product=%s errors=%s — will attempt stale-ID re-match",
                    ctx.tenant_id, external_id, _init_errors,
                )
                # Force the hard-guard below to trigger the re-match path
                _options_payload = []

        # Build args once so Guard B (options re-match retry) can reuse them.
        # When variant_id is resolved locally, send it instead of options.
        def _build_order_args(opts_payload):
            args = {
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
            }
            if _resolved_variant_id:
                args["variant_id"] = _resolved_variant_id
                # Don't send options — variant_id is the single source of truth
            else:
                args["options"] = opts_payload
            return args

        runtime_result = await runtime.execute(
            "create_draft_order", _build_order_args(_options_payload),
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
            try:
                from core.order_creation_evidence import (  # noqa: PLC0415
                    OrderCreationStatus,
                    stamp_order_prep_creation,
                )

                stamp_order_prep_creation(
                    prep,
                    status=OrderCreationStatus.CREATED,
                    salla_order_id=str(order_id),
                )
                prep.last_order_failed = False
            except Exception:  # noqa: BLE001  # noqa: silent-ok — creation status stamp must not block order success
                pass
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

        # ── Salla AUTH failure (401, no refresh, expired token) ───────────────
        # The adapter raised SallaTokenRevokedException with a "salla_auth_failed:"
        # prefix. Do NOT fake a success: tell the customer the merchant must
        # reconnect and stop the order flow.
        if "salla_auth_failed" in error_msg.lower() or "salla token" in error_msg.lower():
            logger.error(
                "[ORDER FLOW] external_create_failed | reason=salla_auth_failed "
                "tenant=%s product=%s err=%s",
                ctx.tenant_id, external_id, error_msg,
            )
            return ActionResult(
                success=True,   # success=True so we still send a customer reply
                data={
                    "external_create_failed":   True,
                    "external_failure_reason":  "salla_auth_failed",
                    "product":                  product_info,
                    "order_prep":               prep.to_dict(),
                    "customer_message":         (
                        "تعذر إنشاء الطلب الآن بسبب مشكلة ربط المتجر. "
                        "بنراجعها ونرجع لك."
                    ),
                },
            )

        # ── Salla rejected customer.mobile format ─────────────────────────────
        # Salla returned 422 with a mobile-format error even though the field
        # was present. Ask the customer to re-send their number in 05XXXXXXXX.
        if "invalid_customer_phone" in error_msg.lower():
            logger.error(
                "[ORDER FLOW] customer.mobile rejected by Salla | tenant=%s product=%s",
                ctx.tenant_id, external_id,
            )
            prep.missing_fields = ["customer_phone"]
            prep.customer_phone = ""  # clear the invalid number so it is re-collected
            return ActionResult(
                success=True,
                data={
                    "product":          product_info,
                    "needs_collection": True,
                    "missing_fields":   ["customer_phone"],
                    "is_first_ask":     False,
                    "question": (
                        "رقم الجوال غير مقبول لدى سلة. "
                        "أرسله بصيغة 05XXXXXXXX (مثال: 0542980511)."
                    ),
                    "external_create_failed":   True,
                    "external_failure_reason":  "invalid_customer_phone",
                    "order_prep": prep.to_dict(),
                },
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
            _slots_to_collect = _filter_missing_phone_if_known(
                _slots_to_collect, ctx.customer_phone,
            )
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
        # needs options.
        #
        # CRITICAL FIX: Do NOT blindly clear prep.product_options — the
        # customer may have already picked the correct values, but we sent
        # them in the wrong format (e.g. value_name string instead of
        # value_id integer). Clearing forces the customer to repeat the
        # selection with no benefit and creates an infinite loop.
        #
        # Strategy:
        #   1. Keep existing selections (prep.product_options) intact.
        #   2. Force-reload metadata (option group IDs / value IDs) from
        #      Salla so we have fresh numeric IDs for the next attempt.
        #   3. Try to re-match the already-selected value names against the
        #      newly loaded metadata to fill in any missing numeric IDs.
        #   4. Only clear selections + re-ask if we truly have NO metadata
        #      at all (product has options but we can't fetch the details).
        _options_missing_signal = (
            "required_product_options_missing" in error_msg
            or "options" in error_msg.lower()
            or "خيارات المنتج" in error_msg
        )
        if _options_missing_signal:
            prep.product_has_required_options = True
            _prev_selections = dict(prep.product_options or {})
            # Force reload: drop cached meta so _ensure_product_options_loaded
            # re-fetches from Salla (fresh IDs). Keep product_options so we
            # can try to re-match existing selections against the new meta.
            prep.product_options_meta = []
            prep.product_options_loaded = False
            logger.error(
                "[ORDER FLOW] options required by Salla — reloading metadata "
                "(keeping %d existing selections) | tenant=%s product=%s "
                "prev_selections=%s",
                len(_prev_selections), ctx.tenant_id, external_id,
                {k: v.get("value_name") for k, v in _prev_selections.items()},
            )
            await _ensure_product_options_loaded(prep, ctx, external_id)

            # Re-match previously selected value names against fresh metadata.
            # This repairs the case where the customer already picked the right
            # value but we had stale/missing IDs at the time we built the payload.
            if _prev_selections and prep.product_options_meta:
                prep.product_options = {}
                for group in prep.product_options_meta:
                    gname = (group.get("name") or "").strip()
                    gkey = gname.lower()
                    prev_sel = _prev_selections.get(gkey)
                    if not prev_sel:
                        continue
                    prev_vname = (prev_sel.get("value_name") or "").strip().lower()
                    for val in group.get("values") or []:
                        vname = (val.get("name") or "").strip()
                        if vname.lower() == prev_vname or _norm_ar(vname.lower()) == _norm_ar(prev_vname):
                            prep.product_options[gkey] = {
                                "option_id": group.get("id"),
                                "option_name": gname,
                                "value_id": val.get("id"),
                                "value_name": vname,
                            }
                            logger.info(
                                "[ORDER FLOW] re-matched option after Salla rejection → "
                                "group=%r value=%r option_id=%s value_id=%s",
                                gname, vname, group.get("id"), val.get("id"),
                            )
                            break
            elif not _prev_selections:
                # No previous selections → truly need to ask customer.
                prep.product_options = {}

            _missing_now = _missing_product_options(prep)
            # Fallback: only use full meta when NOTHING was collected yet.
            # Do NOT use full meta when the customer already selected values —
            # that would incorrectly re-show already-chosen groups.
            if not _missing_now and not prep.product_options:
                _missing_now = list(prep.product_options_meta or [])

            # Guard A: options required but metadata could not be loaded at all.
            if not _missing_now and not prep.product_options_meta:
                logger.warning(
                    "[ORDER FLOW] options required by Salla but metadata unavailable "
                    "after reload — falling to salla_retry/escalate | "
                    "tenant=%s product=%s",
                    ctx.tenant_id, external_id,
                )
                prep.salla_failure_count = (prep.salla_failure_count or 0) + 1
                # Fall through to retry/escalate handling below.

            # Guard B: ALL options are already selected — Salla rejected due to
            # stale IDs.  Re-matched IDs are now fresh; retry immediately.
            elif not _missing_now and prep.product_options:
                _gb_attempt = (prep.salla_failure_count or 0)

                # ── Retry guard ───────────────────────────────────────────────
                if _gb_attempt >= 2:
                    logger.error(
                        "[ORDER FLOW] retry aborted | reason=guard_b_limit "
                        "attempts=%d tenant=%s product=%s — escalating to customer",
                        _gb_attempt, ctx.tenant_id, external_id,
                    )
                    prep.salla_failure_count = _gb_attempt + 1
                    # Fall through to escalate path below.
                else:
                    logger.warning(
                        "[ORDER FLOW] re-match triggered | reason=salla_options_rejected "
                        "attempt=%d tenant=%s product=%s selected=%s",
                        _gb_attempt + 1, ctx.tenant_id, external_id,
                        {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
                    )
                    # Strict re-match with confidence logging
                    _gb_rematched, _gb_conf = _rematch_options(
                        prep.product_options, prep.product_options_meta or [], external_id,
                    )
                    for _gbk, _gbc in _gb_conf.items():
                        _gbinp = (prep.product_options.get(_gbk) or {}).get("value_name", "")
                        _gbmid = (_gb_rematched.get(_gbk) or {}).get("value_id")
                        logger.info(
                            "[ORDER FLOW] re-match result | input=%r matched_id=%s "
                            "confidence=%s group=%r tenant=%s",
                            _gbinp, _gbmid, _gbc, _gbk, ctx.tenant_id,
                        )
                    prep.product_options = _gb_rematched
                    _retry_payload = _resolve_options_payload(prep)
                    _rp_ok, _rp_errors = _validate_options_payload(_retry_payload)

                    if _retry_payload and _rp_ok:
                        logger.info(
                            "[ORDER FLOW] final payload validated | tenant=%s product=%s "
                            "payload=%s",
                            ctx.tenant_id, external_id, _retry_payload,
                        )
                        logger.info(
                            "[ORDER FLOW] entering create_order (auto-retry after re-match) | "
                            "tenant=%s product=%s attempt=%d",
                            ctx.tenant_id, external_id, _gb_attempt + 1,
                        )
                        try:
                            _retry_result = await runtime.execute(
                                "create_draft_order", _build_order_args(_retry_payload),
                            )
                            _retry_order = (_retry_result.payload or {}).get("order")
                            if _retry_order:
                                _r_id = _retry_order.get("id") or ""
                                _r_url = (
                                    _retry_order.get("payment_link")
                                    or _retry_order.get("payment_url")
                                    or _retry_order.get("checkout_url")
                                    or ((_retry_order.get("urls") or {}).get("payment"))
                                    or ""
                                )
                                logger.info(
                                    "[ORDER FLOW] order created (auto-retry) | "
                                    "order_id=%s attempt=%d tenant=%s",
                                    _r_id, _gb_attempt + 1, ctx.tenant_id,
                                )
                                return ActionResult(
                                    success=True,
                                    data={
                                        "order_id":    _r_id,
                                        "reference":   _retry_order.get("reference_id") or _r_id,
                                        "checkout_url": _r_url,
                                        "total":       _retry_order.get("total"),
                                        "currency":    _retry_order.get("currency", "SAR"),
                                        "product":     product_info,
                                        "order_prep":  prep.to_dict(),
                                    },
                                )
                            logger.error(
                                "[ORDER FLOW] auto-retry failed — no order in response | "
                                "attempt=%d tenant=%s err=%s",
                                _gb_attempt + 1, ctx.tenant_id, _retry_result.error,
                            )
                        except Exception as _retry_exc:
                            logger.error(
                                "[ORDER FLOW] auto-retry exception | attempt=%d "
                                "tenant=%s err=%s",
                                _gb_attempt + 1, ctx.tenant_id, _retry_exc,
                            )
                    else:
                        logger.error(
                            "[ORDER FLOW] re-match failed | reason=invalid_payload "
                            "errors=%s attempt=%d tenant=%s",
                            _rp_errors, _gb_attempt + 1, ctx.tenant_id,
                        )
                    # Guard B failed — fall through to escalate path below
                    prep.salla_failure_count = _gb_attempt + 1
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
                try:
                    from core.order_creation_evidence import (  # noqa: PLC0415
                        OrderCreationStatus,
                        stamp_order_prep_creation,
                    )

                    stamp_order_prep_creation(
                        prep, status=OrderCreationStatus.CREATING,
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — creation status stamp must not block retry path
                    pass
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
                try:
                    from core.order_creation_evidence import (  # noqa: PLC0415
                        OrderCreationStatus,
                        stamp_order_prep_creation,
                    )

                    stamp_order_prep_creation(
                        prep, status=OrderCreationStatus.FAILED,
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — creation status stamp must not block escalate path
                    pass
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
        try:
            from core.order_creation_evidence import (  # noqa: PLC0415
                OrderCreationStatus,
                stamp_order_prep_creation,
            )

            stamp_order_prep_creation(prep, status=OrderCreationStatus.FAILED)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — creation status stamp must not block intent-only path
            pass
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


def _looks_like_phone_name(text: str) -> bool:
    if not text:
        return False
    digits = text.lstrip("+").replace(" ", "").replace("-", "")
    return digits.isdigit() and len(digits) >= 7


def _prep_has_real_name(prep: OrderPreparationState) -> bool:
    first = str(prep.customer_first_name or "").strip()
    last = str(prep.customer_last_name or "").strip()
    if first and not _looks_like_phone_name(first):
        return True
    return bool(last and not _looks_like_phone_name(last))


def _should_patch_customer_first(prep: OrderPreparationState, incoming: str) -> bool:
    incoming = str(incoming or "").strip()
    if not incoming or _looks_like_phone_name(incoming):
        return False
    existing = str(prep.customer_first_name or "").strip()
    if existing and not _looks_like_phone_name(existing):
        return False
    return True


def _should_patch_customer_last(prep: OrderPreparationState, incoming: str) -> bool:
    incoming = str(incoming or "").strip()
    if not incoming or _looks_like_phone_name(incoming):
        return False
    existing = str(prep.customer_last_name or "").strip()
    if existing and not _looks_like_phone_name(existing):
        return False
    return True


def _filter_missing_phone_if_known(
    missing: List[str],
    customer_phone: Optional[str],
) -> List[str]:
    """Drop ``customer_phone`` slot when WhatsApp already supplied the number."""
    if not missing:
        return missing
    phone = str(customer_phone or "").strip()
    if not phone:
        return missing
    return [slot for slot in missing if slot != "customer_phone"]


def _seed_checkout_state(prep: OrderPreparationState, ctx: BrainContext) -> None:
    from core.customer_identity_resolver import (  # noqa: PLC0415
        can_use_name_for_operations,
        read_customer_identity,
    )

    profile_name = str(ctx.profile.get("name") or "").strip()
    customer_row = getattr(ctx, "_customer_row", None)
    if customer_row is None:
        try:
            db = getattr(ctx, "_db", None)
            if db and ctx.customer_phone:
                from models import Customer  # noqa: PLC0415
                from utils.phone_utils import normalize_to_e164  # noqa: PLC0415

                e164 = normalize_to_e164(str(ctx.customer_phone))
                customer_row = (
                    db.query(Customer)
                    .filter(
                        Customer.tenant_id == ctx.tenant_id,
                        Customer.normalized_phone == e164,
                    )
                    .first()
                )
        except Exception:  # noqa: BLE001
            customer_row = None

    official_profile_name = ""
    if customer_row is not None and can_use_name_for_operations(customer_row):
        official_profile_name = read_customer_identity(customer_row).customer_name
    elif profile_name:
        from core.customer_name_validator import validate_customer_name  # noqa: PLC0415

        if validate_customer_name(profile_name).valid:
            official_profile_name = profile_name

    first, last = _split_name(official_profile_name)
    if not prep.customer_first_name and first and not _looks_like_phone_name(first):
        prep.customer_first_name = first
    if not prep.customer_last_name and last:
        prep.customer_last_name = last
    if not prep.customer_email:
        prep.customer_email = str(ctx.profile.get("email") or "").strip()
    if not prep.customer_phone:
        prep.customer_phone = str(ctx.customer_phone or "").strip()


def _merge_message_details(prep: OrderPreparationState, slots: dict, message: str) -> None:
    from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots

    slots = dict(slots or {})

    if not (
        slots.get("customer_first_name")
        or slots.get("customer_name")
        or slots.get("full_name")
        or slots.get("first_name")
    ):
        extracted = extract_ordering_slots(message) or {}
        for key in (
            "customer_name",
            "customer_first_name",
            "customer_last_name",
            "city",
            "short_address_code",
            "google_maps_url",
            "latitude",
            "longitude",
        ):
            if extracted.get(key) and not slots.get(key):
                slots[key] = extracted[key]

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

    if _should_patch_customer_first(prep, first_name):
        if first_name != prep.customer_first_name:
            logger.info(
                "[ORDER_NAME_PATCH] field=customer_first_name before=%r after=%r "
                "source=merge_message_details",
                prep.customer_first_name,
                first_name,
            )
        prep.customer_first_name = first_name
    if _should_patch_customer_last(prep, last_name):
        if last_name != prep.customer_last_name:
            logger.info(
                "[ORDER_NAME_PATCH] field=customer_last_name before=%r after=%r "
                "source=merge_message_details",
                prep.customer_last_name,
                last_name,
            )
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
    from core.customer_name_validator import validate_customer_name  # noqa: PLC0415

    parts = [prep.customer_first_name.strip(), prep.customer_last_name.strip()]
    name = " ".join(part for part in parts if part)
    prov = dict(getattr(prep, "identity_provenance", None) or {})
    if name and validate_customer_name(name).valid:
        if prov.get("customer_name") in {
            "explicit_customer_statement",
            "confirmation_yes",
        } or prov.get("recipient_name") == "explicit_customer_statement":
            return name
    fb = str(fallback or "").strip()
    if fb and validate_customer_name(fb).valid:
        return fb
    return "عميل"


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
    if prep.product_options_loaded and prep.product_options_meta:
        logger.debug(
            "[ORDER DEBUG] _ensure_product_options_loaded SKIPPED (already loaded) | "
            "product=%s selected_options=%s meta_groups=%d variants_cached=%d",
            external_id,
            {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
            len(prep.product_options_meta or []),
            len(prep.product_variants_raw or []),
        )
        return
    if prep.product_options_loaded and not prep.product_options_meta:
        logger.debug(
            "[ORDER DEBUG] _ensure_product_options_loaded — loaded but meta empty "
            "(simple product, no option groups) | product=%s",
            external_id,
        )
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
        # ── Also fetch raw variants for local variant resolution ──────────
        # Raw variants preserve related_options/related_option_values which
        # are needed to map selected options → variant_id locally, without
        # a second Salla call at order-creation time.
        if prep.product_has_required_options and hasattr(adapter, "get_raw_variants"):
            try:
                raw_variants = await adapter.get_raw_variants(external_id)
                prep.product_variants_raw = raw_variants
                logger.info(
                    "[ORDER FLOW] raw variants cached | tenant=%s product_id=%s "
                    "count=%d",
                    ctx.tenant_id, external_id, len(raw_variants),
                )
            except Exception as _var_exc:
                logger.debug(
                    "[ORDER FLOW] raw variants fetch failed (non-blocking) | "
                    "tenant=%s product=%s err=%s",
                    ctx.tenant_id, external_id, _var_exc,
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
    logger.debug(
        "[ORDER DEBUG] _merge_message_options ENTER | message=%r "
        "existing_keys=%s meta_groups=%s",
        text[:80],
        list((prep.product_options or {}).keys()),
        [g.get("name") for g in (prep.product_options_meta or [])],
    )
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
                _stored_vid = val.get("id")
                _stored_oid = group.get("id")
                if existing:
                    logger.info(
                        "[ORDER FLOW] option updated → group=%r old=%r new=%r "
                        "option_id=%s value_id=%s",
                        gname, existing_value_name, vname, _stored_oid, _stored_vid,
                    )
                else:
                    logger.info(
                        "[ORDER FLOW] option selected → group=%r value=%r "
                        "option_id=%s value_id=%s",
                        gname, vname, _stored_oid, _stored_vid,
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
                "[ORDER FLOW] option selected → group=%r value=%r option_id=%s value_id=%s (numeric pick idx=%d)",
                gname, val.get("name") or "", group.get("id"), val.get("id"), idx,
            )

    if captured:
        logger.error(
            "[ORDER OPTIONS] extracted_from_text=%s | captured=%d total_selected=%s",
            {k: v.get("value_name") for k, v in (prep.product_options or {}).items()
             if k in [gk.lower() for gk in [g.get("name", "") for g in (prep.product_options_meta or [])]]},
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


# ── Intent-Driven Product Options Prediction ─────────────────────────────────

# Structured metric counters emitted as log lines for aggregation.
# Format: [METRIC] counter_name=1 | key=value ...
_PREDICT_METRICS = {
    "predicted":  "options_auto_predicted_count",
    "confirmed":  "options_prediction_confirmed_count",
    "rejected":   "options_prediction_rejected_count",
    "no_predict": "options_prediction_skipped_count",
    "fallback":   "options_prediction_fallback_count",
}


def _emit_predict_metric(metric_key: str, tenant_id: int, **extra: Any) -> None:
    """Emit a structured metric log line for prediction tracking."""
    counter = _PREDICT_METRICS.get(metric_key, metric_key)
    parts = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info("[METRIC] %s=1 | tenant=%s %s", counter, tenant_id, parts)


_CONFIRM_PREDICTION_KEYWORDS = frozenset({
    "نعم", "اي", "ايه", "أيه", "تمام", "تمم", "كمل", "اكمل", "أكمل",
    "موافق", "موافقه", "صح", "صحيح", "حسنا", "حسناً", "حسن",
    "نكمل", "نكمّل", "كملي", "كمّلي", "عليه", "نكمل عليه",
    "أوكي", "أوكيه", "ماشي", "طيب", "زين", "تمّ",
    "ok", "okay", "yes", "sure", "confirm", "go",
})

_REJECT_PREDICTION_KEYWORDS = frozenset({
    "لا", "لأ", "غير", "غيّر", "غيري", "بدل", "بدّل", "أبغى أغير",
    "ابغى اغير", "ابي اغير", "أبي أغير", "تغيير", "مو هذا",
    "no", "change", "nope",
})

_SAME_AS_BEFORE_KEYWORDS = frozenset({
    "نفس السابق", "نفس الخيارات", "نفس اللي قبل", "زي المرة اللي فاتت",
    "نفس الاختيار", "نفس الطلب", "زي قبل", "نفسها", "نفسه",
})


def _is_option_confirmation(message: str) -> bool:
    """True if the message confirms a prediction."""
    text = (message or "").strip().lower()
    if not text:
        return False
    words = set(text.split())
    if words & _CONFIRM_PREDICTION_KEYWORDS:
        return True
    if text in _CONFIRM_PREDICTION_KEYWORDS:
        return True
    return False


def _is_option_rejection(message: str) -> bool:
    """True if the message rejects/wants to change a prediction."""
    text = (message or "").strip().lower()
    if not text:
        return False
    words = set(text.split())
    return bool(words & _REJECT_PREDICTION_KEYWORDS) or text in _REJECT_PREDICTION_KEYWORDS


def _is_same_as_before(message: str) -> bool:
    """True if the customer wants the same options as their last order."""
    text = (message or "").strip().lower()
    return any(kw in text for kw in _SAME_AS_BEFORE_KEYWORDS)


async def predict_missing_options(
    prep: OrderPreparationState,
    ctx: "BrainContext",
    external_id: str,
    missing_groups: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Predict missing product options from history, popularity, or stock.

    Returns ``{"options": {...}, "source": str, "confidence": float}``
    when a prediction with confidence >= 0.7 is found, or ``None``.

    Sources (tried in order):
      1. last_customer_choice (0.9) — same customer ordered same product before
      2. top_variant          (0.75) — most popular variant across all orders
      3. stock_heavy          (0.6)  — variant with highest stock (below threshold)
    """
    if not missing_groups or not external_id:
        return None

    db = getattr(ctx, "_db", None)
    meta = prep.product_options_meta or []
    if not meta:
        return None

    # Build a name→group lookup for quick mapping from variant data
    _group_by_id: Dict[str, Dict[str, Any]] = {}
    for g in meta:
        gid = g.get("id")
        if gid is not None:
            _group_by_id[str(gid)] = g

    def _variant_id_to_options(variant_id: str) -> Optional[Dict[str, Any]]:
        """Map a variant_id to option selections using product_options_meta
        and Salla's related_options/related_option_values parallel arrays.

        Returns a dict keyed by lowercased group name → selection dict,
        or None if the variant is not in the loaded metadata."""
        # We need the product's variants to resolve the mapping.
        # The adapter's get_product already loaded them; check if they're
        # available via product_options_meta or the prep's cached product.
        # Since we don't store full variant data on prep, we'll try to
        # query from the adapter.
        return None  # handled below per-source

    # ── Source 1: Last customer choice ────────────────────────────────────
    if db and ctx.customer_id:
        try:
            from models import Order  # noqa: PLC0415
            from sqlalchemy import text as _text  # noqa: PLC0415

            recent_order = (
                db.query(Order)
                .filter(
                    Order.tenant_id == ctx.tenant_id,
                    Order.line_items.isnot(None),
                )
                .filter(
                    Order.customer_info["phone"].astext != "",
                )
                .order_by(Order.id.desc())
                .limit(20)
                .all()
            )

            customer_phone_digits = "".join(
                c for c in (ctx.customer_phone or "") if c.isdigit()
            )[-9:]

            for order in recent_order:
                # Match by phone suffix (last 9 digits)
                order_phone = ""
                if isinstance(order.customer_info, dict):
                    order_phone = str(order.customer_info.get("phone", "") or "")
                order_phone_digits = "".join(c for c in order_phone if c.isdigit())[-9:]
                if not order_phone_digits or order_phone_digits != customer_phone_digits:
                    continue

                items = order.line_items if isinstance(order.line_items, list) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_product_id = str(
                        item.get("product_id") or item.get("id") or ""
                    )
                    if item_product_id != external_id:
                        continue

                    # Found a past order for the same product by this customer
                    item_options = item.get("options") or []
                    if not item_options and not item.get("variant_id"):
                        continue

                    # Try to extract options from the raw Salla line item
                    predicted: Dict[str, Any] = {}
                    for opt in (item_options if isinstance(item_options, list) else []):
                        if not isinstance(opt, dict):
                            continue
                        opt_name = str(opt.get("name") or "").strip().lower()
                        opt_value = str(
                            opt.get("value") or opt.get("value_name") or ""
                        ).strip()
                        if not opt_name or not opt_value:
                            continue
                        # Only predict groups that are actually missing
                        for mg in missing_groups:
                            mg_key = (mg.get("name") or "").strip().lower()
                            if mg_key == opt_name:
                                # Find the matching value in meta
                                for v in mg.get("values") or []:
                                    if (v.get("name") or "").strip().lower() == opt_value.lower():
                                        predicted[mg_key] = {
                                            "option_id": mg.get("id"),
                                            "option_name": (mg.get("name") or "").strip(),
                                            "value_id": v.get("id"),
                                            "value_name": (v.get("name") or "").strip(),
                                        }
                                        break

                    if predicted and len(predicted) == len(missing_groups):
                        logger.error(
                            "[ORDER OPTIONS PREDICT] product_id=%s source=last_customer_choice "
                            "confidence=0.9 predicted=%s | tenant=%s customer=%s",
                            external_id, {k: v.get("value_name") for k, v in predicted.items()},
                            ctx.tenant_id, ctx.customer_id,
                        )
                        return {
                            "options": predicted,
                            "source": "last_customer_choice",
                            "confidence": 0.9,
                        }
        except Exception as exc:
            logger.debug(
                "[ORDER OPTIONS PREDICT] source=last_customer_choice failed | err=%s",
                exc,
            )

    # ── Source 2: Top-selling variant ─────────────────────────────────────
    if db:
        try:
            from models import Order  # noqa: PLC0415
            from collections import Counter

            orders = (
                db.query(Order)
                .filter(
                    Order.tenant_id == ctx.tenant_id,
                    Order.line_items.isnot(None),
                )
                .order_by(Order.id.desc())
                .limit(100)
                .all()
            )

            variant_counter: Counter = Counter()
            variant_options_cache: Dict[str, List] = {}
            for order in orders:
                items = order.line_items if isinstance(order.line_items, list) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_pid = str(item.get("product_id") or item.get("id") or "")
                    if item_pid != external_id:
                        continue
                    vid = str(item.get("variant_id") or "")
                    if vid:
                        variant_counter[vid] += 1
                        if vid not in variant_options_cache:
                            variant_options_cache[vid] = item.get("options") or []

            if variant_counter:
                top_vid, _count = variant_counter.most_common(1)[0]
                top_options_raw = variant_options_cache.get(top_vid) or []

                predicted: Dict[str, Any] = {}
                for opt in (top_options_raw if isinstance(top_options_raw, list) else []):
                    if not isinstance(opt, dict):
                        continue
                    opt_name = str(opt.get("name") or "").strip().lower()
                    opt_value = str(
                        opt.get("value") or opt.get("value_name") or ""
                    ).strip()
                    if not opt_name or not opt_value:
                        continue
                    for mg in missing_groups:
                        mg_key = (mg.get("name") or "").strip().lower()
                        if mg_key == opt_name:
                            for v in mg.get("values") or []:
                                if (v.get("name") or "").strip().lower() == opt_value.lower():
                                    predicted[mg_key] = {
                                        "option_id": mg.get("id"),
                                        "option_name": (mg.get("name") or "").strip(),
                                        "value_id": v.get("id"),
                                        "value_name": (v.get("name") or "").strip(),
                                    }
                                    break

                if predicted and len(predicted) == len(missing_groups):
                    logger.error(
                        "[ORDER OPTIONS PREDICT] product_id=%s source=top_variant "
                        "confidence=0.75 predicted=%s top_vid=%s count=%d | tenant=%s",
                        external_id,
                        {k: v.get("value_name") for k, v in predicted.items()},
                        top_vid, _count, ctx.tenant_id,
                    )
                    return {
                        "options": predicted,
                        "source": "top_variant",
                        "confidence": 0.75,
                    }
        except Exception as exc:
            logger.debug(
                "[ORDER OPTIONS PREDICT] source=top_variant failed | err=%s", exc,
            )

    # ── Source 3: Stock-heavy option ──────────────────────────────────────
    # Pick the first value of each missing group (Salla typically returns
    # values ordered by popularity/stock). Confidence is below the 0.7
    # threshold, so this source is only used as a weak signal.
    # We keep it at 0.6 to match the spec — callers skip it unless the
    # threshold is explicitly lowered.
    if len(missing_groups) == 1:
        mg = missing_groups[0]
        values = mg.get("values") or []
        if len(values) == 1:
            # Only one possible value — no ambiguity at all
            v = values[0]
            predicted = {
                (mg.get("name") or "").strip().lower(): {
                    "option_id": mg.get("id"),
                    "option_name": (mg.get("name") or "").strip(),
                    "value_id": v.get("id"),
                    "value_name": (v.get("name") or "").strip(),
                }
            }
            logger.error(
                "[ORDER OPTIONS PREDICT] product_id=%s source=stock_heavy "
                "confidence=0.95 predicted=%s reason=single_value_only | tenant=%s",
                external_id,
                {k: v_sel.get("value_name") for k, v_sel in predicted.items()},
                ctx.tenant_id,
            )
            return {
                "options": predicted,
                "source": "stock_heavy",
                "confidence": 0.95,
            }

    logger.info(
        "[ORDER OPTIONS PREDICT] no prediction possible | product_id=%s "
        "missing_groups=%s tenant=%s",
        external_id, [g.get("name") for g in missing_groups], ctx.tenant_id,
    )
    return None


def _resolve_variant_locally(
    prep: OrderPreparationState,
    external_id: str,
    tenant_id: int,
) -> Optional[str]:
    """Resolve variant_id locally from cached raw variants.

    Maps the customer's selected options (option_id → value_id) against
    each cached variant's ``related_options``/``related_option_values``
    parallel arrays.  Returns the matching variant_id or None.

    This MUST succeed before any order is sent to Salla when the product
    has variants.  No remote calls are made — purely local lookup.
    """
    if not prep.product_variants_raw:
        return None
    if not prep.product_options:
        return None

    wanted: Dict[str, str] = {}
    for sel in prep.product_options.values():
        if not isinstance(sel, dict):
            continue
        oid = sel.get("option_id")
        vid = sel.get("value_id")
        if oid is not None and vid is not None:
            wanted[str(oid)] = str(vid)

    if not wanted:
        return None

    for variant in prep.product_variants_raw:
        if not isinstance(variant, dict):
            continue
        options = variant.get("related_options") or []
        values = variant.get("related_option_values") or []
        if not isinstance(options, list) or not isinstance(values, list):
            continue
        if len(options) != len(values):
            continue
        variant_map = {str(o): str(v) for o, v in zip(options, values)}

        logger.debug(
            "[VARIANT LOCAL] candidate | variant_id=%s map=%s wanted=%s",
            variant.get("id"), variant_map, wanted,
        )

        if variant_map == wanted:
            matched_id = str(variant.get("id"))
            logger.error(
                "[VARIANT LOCAL] matched | variant_id=%s product=%s "
                "wanted=%s tenant=%s",
                matched_id, external_id, wanted, tenant_id,
            )
            return matched_id

    logger.error(
        "[ORDER BLOCKED] reason=no_variant_match | product=%s "
        "selected_options=%s wanted=%s available_variants=%d tenant=%s",
        external_id,
        {k: v.get("value_name") for k, v in (prep.product_options or {}).items()},
        wanted,
        len(prep.product_variants_raw),
        tenant_id,
    )
    return None


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


# ── Option ID re-match cache ───────────────────────────────────────────────
# Keyed by (product_external_id, group_key, value_name_lower) → full entry dict.
# Survives across turns for the same worker process; cleared on product change
# (DraftOrderHandler resets prep.product_options on product_changed).
_OPTION_ID_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}


def _rematch_options(
    prev_options: Dict[str, Any],
    meta: List[Dict[str, Any]],
    product_id: str,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Re-match saved option value names against freshly-loaded Salla metadata.

    Returns ``(new_product_options, confidence_map)``.

    Matching is STRICT — three tiers, no fuzzy/substring:
      1. ``exact``            — case-insensitive match (``v.lower() == saved.lower()``)
      2. ``arabic_normalized``— after stripping diacritics / unifying alef forms
      3. ``none``             — no match found → group is NOT included in result

    The caller must treat ``confidence="none"`` groups as unresolved and ask the
    customer again rather than guessing.
    """
    new_opts: Dict[str, Any] = {}
    confidence: Dict[str, str] = {}

    for group in meta:
        gname = (group.get("name") or "").strip()
        gkey  = gname.lower()
        prev  = prev_options.get(gkey)
        if not prev:
            confidence[gkey] = "none"
            continue

        prev_vname  = (prev.get("value_name") or "").strip()
        prev_lower  = prev_vname.lower()
        prev_norm   = _norm_ar(prev_lower)

        # Fast path: check the in-process cache
        _cache_key = (product_id, gkey, prev_lower)
        if _cache_key in _OPTION_ID_CACHE:
            new_opts[gkey] = _OPTION_ID_CACHE[_cache_key]
            confidence[gkey] = "cached"
            continue

        best_match: Optional[Dict[str, Any]] = None
        best_conf  = "none"

        for val in group.get("values") or []:
            vname   = (val.get("name") or "").strip()
            v_lower = vname.lower()
            v_norm  = _norm_ar(v_lower)

            if v_lower == prev_lower:
                best_match = val
                best_conf  = "exact"
                break                           # nothing better than exact
            if v_norm == prev_norm and best_conf == "none":
                best_match = val
                best_conf  = "arabic_normalized"
                # keep scanning in case an exact match appears later

        if best_match is not None:
            entry = {
                "option_id":   group.get("id"),
                "option_name": gname,
                "value_id":    best_match.get("id"),
                "value_name":  best_match.get("name") or prev_vname,
            }
            new_opts[gkey]  = entry
            confidence[gkey] = best_conf
            _OPTION_ID_CACHE[_cache_key] = entry   # populate cache
        else:
            confidence[gkey] = "none"

    return new_opts, confidence


def _validate_options_payload(
    payload: List[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """Validate that every entry in the options payload has real IDs.

    Returns ``(ok, error_list)``.  ``error_list`` is empty when ``ok=True``.
    """
    errors: List[str] = []
    for entry in payload:
        if entry.get("option_id") is None:
            errors.append(
                f"option_id is None for group={entry.get('option_name')!r}"
            )
        if entry.get("value_id") is None:
            errors.append(
                f"value_id is None for value={entry.get('value_name')!r} "
                f"in group={entry.get('option_name')!r}"
            )
    return (len(errors) == 0, errors)

