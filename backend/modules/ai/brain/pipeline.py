"""
brain/pipeline.py
──────────────────
MerchantBrain — the Phase 1 Commerce Decision Engine.

Turn processing flow:
  message → IntentClassifier → StateStore.load → FactsLoader.load
          → BrainContext assembly
          → DecisionEngine.decide
          → PolicyGate.gate
          → ActionExecutor.execute
          → projected Brain state + SuggestionEngine
          → Composer.compose
          → StateStore.save
          → MemoryUpdater.update
          → reply string

The build_default_brain() factory wires all Phase 1 default implementations
together. Any layer can be replaced by passing a different implementation.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .types import (
    ActionResult,
    BrainReplyState,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SalesContextSnapshot,
    SuggestionSnapshot,
    INTENT_GENERAL,
    INTENT_PICK_LIST_ITEM,
)
from .decision.actions import (
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_OUT_OF_SCOPE,
    ACTION_PLATFORM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SOCIAL_REPLY,
)
from .protocols import (
    IntentClassifier,
    StateStore,
    FactsLoader,
    DecisionMaker,
    PolicyGate,
    ActionExecutor,
    SuggestionEngine,
    Composer,
    MemoryUpdater,
)

logger = logging.getLogger("nahla.brain.pipeline")


# ── Catalog-order focus pinning (June 2026) ─────────────────────────────
# Helpers for ``_maybe_pin_catalog_focus``. Defined at module scope so
# regex objects compile once and tests can import them directly.
import re as _re_catalog  # noqa: E402

_CATALOG_FRAME_MARKER = "[طلب كتالوج من العميل]"
_CATALOG_SKU_RE      = _re_catalog.compile(r"رمز المنتج \(SKU\):\s*(\S+)")
_CATALOG_TOTAL_RE    = _re_catalog.compile(r"الإجمالي:\s*([0-9]+(?:\.[0-9]+)?)\s*(\S+)?")
_CATALOG_QTY_RE      = _re_catalog.compile(r"عدد المنتجات:\s*(\d+)")
# Captures the human-readable product label the normalizer extracts
# from the catalog payload when the BSP includes a name field.
# Stops at end-of-line so a multi-product join (" + ") stays intact
# but the trailing buying-intent paragraph does not leak in.
_CATALOG_NAME_RE     = _re_catalog.compile(r"^اسم المنتج:\s*(.+?)\s*$", _re_catalog.MULTILINE)


def _maybe_pin_catalog_focus(
    *,
    db: Any,
    tenant_id: int,
    message: Optional[str],
    state: MerchantConversationState,
) -> None:
    """
    If ``message`` is a catalog-order frame produced by
    ``modules.ai.media.normalizer`` AND the conversation does not yet
    have a ``current_product_focus``, stamp the focus so the decision
    engine will not collapse into ``ACTION_STASH_ADDRESS_PRE_PRODUCT``
    on the customer's next address turn.

    Failure to resolve the SKU against the catalog is **not** fatal —
    we still pin a placeholder focus so the address-stash gate trips.
    The brain's reply text is not affected by this function in any
    way; we only set state flags that the existing decision engine
    already understands.
    """
    if not message or _CATALOG_FRAME_MARKER not in message:
        return
    if state.current_product_focus:
        return

    sku_m   = _CATALOG_SKU_RE.search(message)
    total_m = _CATALOG_TOTAL_RE.search(message)
    qty_m   = _CATALOG_QTY_RE.search(message)
    name_m  = _CATALOG_NAME_RE.search(message)

    sku      = (sku_m.group(1).strip() if sku_m else "") or ""
    qty      = int(qty_m.group(1)) if qty_m else 1
    currency = (total_m.group(2).strip() if (total_m and total_m.group(2)) else "")
    payload_name = (name_m.group(1).strip() if name_m else "") or ""
    try:
        total_price = float(total_m.group(1)) if total_m else None
    except (TypeError, ValueError):
        total_price = None
    unit_price: Optional[float] = None
    if total_price is not None and qty > 0:
        unit_price = round(total_price / qty, 2)

    # Best-effort lookup against the merchant's catalog so we can populate
    # title / numeric id. Any failure is swallowed — the placeholder pin
    # below is enough to gate the address-stash branch.
    resolved_id: Any = None
    resolved_title: str = ""
    resolved_price: Optional[float] = None
    if sku:
        try:
            from database.models import Product  # noqa: PLC0415
            row = (
                db.query(Product)
                  .filter(Product.tenant_id == tenant_id)
                  .filter(
                      (Product.external_id      == sku) |
                      (Product.sku              == sku) |
                      (Product.meta_retailer_id == sku)
                  )
                  .first()
            )
            if row is not None:
                resolved_id    = getattr(row, "id", None)
                resolved_title = getattr(row, "title", "") or ""
                _p = getattr(row, "price", None)
                if _p is not None:
                    try:
                        resolved_price = float(_p)
                    except (TypeError, ValueError):
                        resolved_price = None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CATALOG_FOCUS] product lookup failed tenant=%s sku=%r: %s",
                tenant_id, sku, exc,
            )

    # Title selection priority:
    #   1. Real product row resolved from the merchant's catalog DB.
    #   2. Human-readable name forwarded by WhatsApp / the BSP in the
    #      order payload (parsed back out of the framed text by the
    #      ``_CATALOG_NAME_RE`` regex above).
    #   3. Empty string — let the LLM speak from price + context.
    final_title = resolved_title or payload_name or ""

    state.current_product_focus = {
        "id":           resolved_id if resolved_id is not None else (sku or "catalog_order"),
        "external_id":  sku,
        "title":        final_title,
        "price":        resolved_price if resolved_price is not None else unit_price,
        "currency":     currency,
        "from_catalog_order": True,
    }
    logger.info(
        "[CATALOG_FOCUS] pinned current_product_focus from catalog order | "
        "tenant=%s sku=%r resolved=%s payload_name=%r qty=%s total=%s currency=%r",
        tenant_id, sku, bool(resolved_id), payload_name, qty, total_price, currency,
    )


class MerchantBrain:
    """
    Orchestrates all Brain layers for a single customer turn.
    """

    def __init__(
        self,
        classifier: IntentClassifier,
        state_store: StateStore,
        facts_loader: FactsLoader,
        decision_engine: DecisionMaker,
        policy_gate: PolicyGate,
        executor: ActionExecutor,
        composer: Composer,
        memory_updater: MemoryUpdater,
        suggestion_engine: Optional[SuggestionEngine] = None,
        sales_context_loader: Optional[Any] = None,
    ) -> None:
        self._classifier     = classifier
        self._state_store    = state_store
        self._facts_loader   = facts_loader
        self._decision_engine= decision_engine
        self._policy_gate    = policy_gate
        self._executor       = executor
        self._composer       = composer
        self._memory_updater = memory_updater
        if sales_context_loader is None:
            from .facts.sales_context import DefaultSalesContextLoader
            sales_context_loader = DefaultSalesContextLoader()
        self._sales_context_loader = sales_context_loader
        if suggestion_engine is None:
            from .suggestion.engine import DefaultSuggestionEngine
            suggestion_engine = DefaultSuggestionEngine()
        self._suggestion_engine = suggestion_engine

    async def process(
        self,
        db: Any,
        tenant_id: int,
        customer_phone: str,
        message: str,
        history: List[Dict[str, Any]],
        profile: Dict[str, Any],
        customer_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
        tenant_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        t0 = time.monotonic()

        # ── Outbound billing guard ────────────────────────────────────────────
        # Inbound message ingestion and conversation recording always run.
        # AI REPLY is an outbound action — blocked when no active billing.
        from core.billing import has_billing_access as _has_access  # noqa: PLC0415
        if not _has_access(db, tenant_id):
            logger.info(
                "[Brain] billing_access_denied — recording inbound but skipping AI reply | tenant=%s",
                tenant_id,
            )
            return {
                "reply":   None,
                "skipped": True,
                "reason":  "billing_access_denied",
            }

        # ── 0. Tenant isolation context (single source of truth for the turn) ─
        # Built once here. Every downstream layer (sales context loader,
        # handlers/runtime, memory updater, signal emitter) MUST reuse this
        # object instead of re-deriving the tenant id from raw inputs.
        from modules.ai.security import (
            TenantIsolationLayer,
            TenantIsolationViolation,
        )

        try:
            if tenant_context is not None:
                TenantIsolationLayer.assert_active(tenant_context)
                if int(tenant_context.tenant_id) != int(tenant_id):
                    raise TenantIsolationViolation(
                        f"tenant_context.tenant_id={tenant_context.tenant_id} "
                        f"does not match process(tenant_id={tenant_id})"
                    )
                tenant_ctx = tenant_context
            else:
                tenant_ctx = TenantIsolationLayer.make_context(
                    tenant_id      = tenant_id,
                    customer_phone = customer_phone,
                    customer_id    = customer_id,
                )
        except TenantIsolationViolation:
            # Hard error — never silently degrade tenant safety. The caller
            # surface (router / orchestrator) is responsible for translating
            # this into a user-facing error.
            raise

        # ── 1. Intent ────────────────────────────────────────────────────
        state_for_classify = self._state_store.load(db, tenant_id, customer_phone)

        # ── 1b. Infer greeted from history (catches proactive outbounds) ─
        # If ANY prior outbound exists in history (cart recovery template,
        # automation push, manual agent reply, even our own previous turn
        # when persistence didn't land for some reason), the customer has
        # already heard from us — never re-introduce. This is the catch-net
        # for paths that send messages outside the Brain pipeline and forget
        # to call StateStore.mark_greeted.
        if not state_for_classify.greeted and _history_has_outbound(history):
            logger.info(
                "[BrainPipeline] inferred greeted=True from history "
                "(prior outbound found) tenant=%s",
                tenant_id,
            )
            state_for_classify.greeted = True

        # ── 1c. Catalog-order product-focus pin (June 2026) ──────────────
        # When the customer submits a WhatsApp catalog order, the inbound
        # text is framed by ``modules.ai.media.normalizer`` with a fixed
        # marker. We use that marker as a deterministic signal that the
        # customer has already chosen a product, and we pin
        # ``state.current_product_focus`` BEFORE the decision engine runs.
        #
        # Why this exists: without a focus stamp, the very next turn
        # (e.g. customer shares a national-address code) collapses into
        # ``ACTION_STASH_ADDRESS_PRE_PRODUCT`` because the decision-engine
        # gate checks ``not state.current_product_focus``. That branch
        # tells the customer "اختر المنتج أول" — which is wrong when a
        # catalog order already declared the product.
        #
        # Surgical contract:
        # • We do NOT inject any reply copy.
        # • We do NOT add a new intent or template.
        # • We only flip a state flag the existing decision engine
        #   already understands, so the brain composes naturally.
        # • If a focus is already set, we leave it alone.
        _maybe_pin_catalog_focus(
            db=db,
            tenant_id=tenant_id,
            message=message,
            state=state_for_classify,
        )

        intent: Intent = await self._classifier.classify(message, history, state_for_classify)

        # ── 2. Load state + facts ─────────────────────────────────────────
        state: MerchantConversationState = state_for_classify
        facts: CommerceFacts             = self._facts_loader.load(db, tenant_id)

        # ── 3. Assemble context ───────────────────────────────────────────
        sales_context: SalesContextSnapshot = self._sales_context_loader.load(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            state=state,
            history=history,
            profile=profile,
            customer_id=customer_id,
            tenant_context=tenant_ctx,
        )

        # ── 3b. Merchant context (Step 2 wire-up — best-effort) ───────────
        # Fact-grounded view of catalog + policies + customer + brain profile.
        # If anything fails (DB hiccup, missing settings row, etc.) we keep
        # an empty dict and the brain falls back to its previous behaviour.
        merchant_context: Dict[str, Any] = {}
        try:
            from core.store_knowledge import build_merchant_context  # noqa: PLC0415
            merchant_context = build_merchant_context(
                db,
                tenant_id      = tenant_id,
                customer_phone = customer_phone,
                product_query  = message or "",
                state          = state,
                history        = history,
                profile        = profile,
            ) or {}
        except Exception as exc:
            logger.warning(
                "[BrainPipeline] build_merchant_context failed tenant=%s — "
                "falling back to legacy context: %s",
                tenant_id, exc,
            )
            merchant_context = {}

        ctx = BrainContext(
            tenant_id      = tenant_id,
            customer_phone = customer_phone,
            message        = message,
            intent         = intent,
            state          = state,
            facts          = facts,
            history        = history,
            profile        = profile,
            customer_id    = customer_id,
            conversation_id= conversation_id,
            sales_context  = sales_context,
            tenant_context = tenant_ctx,
            merchant_context = merchant_context,
        )
        # Attach db for handlers that need it (avoids threading Session issues)
        ctx._db = db  # type: ignore[attr-defined]

        if merchant_context:
            logger.info(
                "[BrainPipeline] merchant_context loaded tenant=%s products=%d "
                "policies=%d has_customer=%s",
                tenant_id,
                len(merchant_context.get("products") or []),
                sum(1 for v in (merchant_context.get("policy_presence") or {}).values() if v),
                bool((merchant_context.get("customer") or {}).get("phone")),
            )

        stage_before = state.stage

        # ── 4. Decision ───────────────────────────────────────────────────
        decision: Decision   = self._decision_engine.decide(ctx)
        reason_before_policy = decision.reason
        decision             = self._policy_gate.gate(decision, ctx)

        # Visible in all Railway log levels — critical checkpoint.
        _policy_changed = (decision.reason != reason_before_policy)
        logger.info(
            "[ORDER FLOW] pipeline decision | tenant=%s action=%s "
            "reason=%r policy_changed=%s intent=%s candidates=%d focus=%r",
            ctx.tenant_id,
            decision.action,
            decision.reason,
            _policy_changed,
            ctx.intent.name if ctx.intent else "(none)",
            len(ctx.state.last_search_candidates or []),
            (ctx.state.current_product_focus or {}).get("title"),
        )

        # ── 5. Execute ────────────────────────────────────────────────────
        result: ActionResult = await self._executor.execute(decision, ctx)

        # ── 6. Project next state + suggestion snapshot ───────────────────
        new_state = self._state_store.transition(state, intent, decision)
        # Record the brain action that produced this turn so the
        # `BRAIN_RESULT` log line and `/debug/recent-whatsapp-turns`
        # endpoint can report it without parsing free-form logs.
        new_state.last_action = str(decision.action or "")

        # ── Conversation-context memory (May 2026) ───────────────────────
        # Persist the platform topic so a bare "نعم" on the NEXT turn
        # can be resolved by the decision engine's context-inheritance
        # branch (0z) instead of falling through to a generic greet.
        # Also clear the topic when the conversation transitions to a
        # commerce action so the platform context doesn't bleed into
        # product flows.
        if decision.action == ACTION_PLATFORM_REPLY:
            _platform_topic_now = str(
                (decision.args or {}).get("platform_topic") or "general_platform"
            )
            new_state.last_platform_topic = _platform_topic_now
            new_state.pending_confirmation = "send_platform_link"
        elif decision.action in (
            "search_products",
            ACTION_PROPOSE_DRAFT_ORDER,
            "narrow_results",
            "suggest_coupon",
            "recommend_addon",
        ):
            new_state.last_platform_topic = ""
            new_state.pending_confirmation = ""
        else:
            new_state.pending_confirmation = ""
        if result.data.get("checkout_url"):
            new_state.checkout_url  = result.data["checkout_url"]
            new_state.stage = "checkout"
        if result.data.get("order_id"):
            new_state.draft_order_id = str(result.data["order_id"])
        if result.data.get("product") and (
            decision.action == "search_products"
            or decision.action == ACTION_PROPOSE_DRAFT_ORDER
            or not new_state.current_product_focus
        ):
            new_state.current_product_focus = result.data["product"]
        if result.data.get("order_prep"):
            new_state.order_prep = OrderPreparationState.from_dict(result.data.get("order_prep"))
            # Sync option-selection state to top-level MerchantConversationState
            # fields so the decision engine can read them without importing orders.py.
            _op_opts = new_state.order_prep.product_options or {}
            _op_meta = new_state.order_prep.product_options_meta or []
            new_state.current_selected_options = {
                k: v.get("value_name", "") for k, v in _op_opts.items()
            }
            # pending_option_groups: groups in meta that are NOT yet in product_options
            _selected_keys = set(k.lower() for k in _op_opts.keys())
            new_state.pending_option_groups = [
                (g.get("name") or "").strip()
                for g in _op_meta
                if (g.get("values") or []) and (g.get("name") or "").strip().lower() not in _selected_keys
            ]
            if new_state.pending_option_groups:
                logger.info(
                    "[ORDER FLOW] pending_option_groups=%s selected=%s | tenant=%s",
                    new_state.pending_option_groups,
                    new_state.current_selected_options,
                    tenant_id,
                )
            elif _op_opts:
                logger.info(
                    "[ORDER FLOW] options_pending=[] all options collected=%s | tenant=%s",
                    new_state.current_selected_options, tenant_id,
                )
            # Sync prediction confirmation flag so the decision engine can
            # route the next turn correctly without importing orders.py.
            new_state.awaiting_option_confirmation = bool(
                getattr(new_state.order_prep, "awaiting_option_confirmation", False)
            )
            # Once the order_prep has captured the address values, the
            # pre-product stash has done its job — clear it so a future
            # browsing round doesn't accidentally inject stale codes.
            _op = new_state.order_prep
            if (
                (new_state.pending_short_address_code and _op.short_address_code)
                or (new_state.pending_google_maps_url and _op.google_maps_url)
                or (new_state.pending_city and _op.city)
            ):
                logger.info(
                    "[ORDER FLOW] clearing pre-product address stash (consumed by order_prep) | "
                    "had_short=%s had_maps=%s had_city=%s",
                    bool(new_state.pending_short_address_code),
                    bool(new_state.pending_google_maps_url),
                    bool(new_state.pending_city),
                )
                new_state.pending_short_address_code = ""
                new_state.pending_google_maps_url = ""
                new_state.pending_city = ""

        # Persist address signals captured BEFORE a product was picked
        # (e.g. customer typed "TAPA7401" while still browsing). The
        # next-turn DraftOrderHandler consumes these values without
        # asking for them again. We only OVERWRITE pending_* when the
        # current message actually carried a value, so an older stash
        # survives subsequent address-free turns.
        _stash = result.data.get("stash_address") or {}
        if _stash:
            if _stash.get("short_address_code"):
                new_state.pending_short_address_code = str(_stash["short_address_code"])
            if _stash.get("google_maps_url"):
                new_state.pending_google_maps_url = str(_stash["google_maps_url"])
            if _stash.get("city"):
                new_state.pending_city = str(_stash["city"])
            logger.info(
                "[ORDER FLOW] stashed address pre-product | short_code=%r maps=%r city=%r",
                new_state.pending_short_address_code,
                (new_state.pending_google_maps_url or "")[:60],
                new_state.pending_city,
            )

        # If the executor flagged the focused product as un-syncable on the
        # store (wrong / stale identifier, deleted, no external_id), drop
        # the focus so the next message lets the user pick a different
        # product and we don't loop on the same broken id.
        # Also clear last_search_candidates: the products in that list came
        # from the same catalog sync that produced the broken external_id, so
        # they are equally suspect. Clearing forces the user to trigger a
        # fresh search rather than looping on the same unavailable products.
        if result.data.get("product_unsyncable"):
            logger.warning(
                "[ORDER FLOW] dropping product focus + search candidates — "
                "product unsyncable on store | previous_focus=%s",
                (new_state.current_product_focus or {}).get("title"),
            )
            new_state.current_product_focus = None
            new_state.order_prep = OrderPreparationState()
            new_state.last_search_candidates = []

        # ── 6b. Persist search candidates so user can pick by number ─────────
        # IMPORTANT: the source of truth is the executor (search.py returns
        # `products`). The composer tags a narrower `pending_candidates`
        # for buttons, but it runs AFTER this block — so we must NOT rely on
        # it to populate state. This was the root cause of the production bug
        # where pick_list_item ("اخترت 2") fell through to the LLM because
        # state.last_search_candidates was always empty.
        #
        # We persist the list whenever the executor returned a products array
        # — regardless of decision.action. "Show top sellers" / "recommend"
        # / "search" all surface ordered lists the user can pick from.
        _search_products = (
            result.data.get("pending_candidates")
            or result.data.get("products")
            or result.data.get("recommended_products")
            or []
        )
        if decision.args.get("rejected_product"):
            # Customer picked a product that was not orderable. The decision
            # engine routed to ACTION_SEARCH_PRODUCTS with alternatives.
            # Clear product focus so we don't loop on the rejected product,
            # and replace candidates with the orderable alternatives.
            new_state.current_product_focus = None
            alts = decision.args.get("alternatives") or _search_products
            new_state.last_search_candidates = list(alts)[:16]
            logger.info(
                "[ORDER FLOW] rejected unorderable pick — replaced candidates | "
                "rejected=%r new_count=%d",
                (decision.args["rejected_product"] or {}).get("title"),
                len(new_state.last_search_candidates),
            )
        elif intent.name == INTENT_PICK_LIST_ITEM and new_state.current_product_focus:
            # Successful pick → decision engine already consumed the chosen
            # product into ACTION_PROPOSE_DRAFT_ORDER. Clear candidates.
            new_state.last_search_candidates = []
        elif _search_products:
            # Cap to 16 so picks like "14" remain meaningful (top-seller lists
            # often exceed 8 items) while state stays small.
            new_state.last_search_candidates = list(_search_products)[:16]

            # ── Diagnostic: log what we stored vs what was old focus ──────────
            _old_focus_title = (new_state.current_product_focus or {}).get("title")
            logger.info(
                "[ORDER FLOW] product list displayed | "
                "candidates=%s old_focus=%r action=%s",
                [
                    {
                        "index": _i + 1,
                        "name": _p.get("title"),
                        "external_id": _p.get("external_id"),
                        "can_checkout": _p.get("can_checkout", _p.get("orderable")),
                    }
                    for _i, _p in enumerate(new_state.last_search_candidates[:5])
                ],
                _old_focus_title,
                decision.action,
            )

            # CRITICAL: A new product list is now active.  Clear the stale
            # current_product_focus so that section 3.7 in the decision engine
            # (continuation-intent routing) cannot fire on the OLD focus when
            # the customer sends a number like "1" that should resolve from the
            # NEW candidate list.  Without this, "1" is routed as INTENT_GENERAL
            # + old focus → ACTION_PROPOSE_DRAFT_ORDER with wrong product.
            #
            # EXCEPTION — Conversation Commerce State Tracking:
            # When the customer is in the middle of an active order
            # (stage=ORDERING and order_prep already captured ANY of
            # product_id / city / customer name / short address code /
            # maps link), wiping focus here breaks the funnel.  The
            # merchant reported: "customer sent national address, bot
            # said 'اختر المنتج اللي تبغاه'" — that's exactly this race.
            # Keep the focus, ignore the list-display side-effect.
            _op = getattr(new_state, "order_prep", None)
            _has_live_order = (
                new_state.stage in ("ordering", "deciding")
                and _op is not None
                and any((
                    getattr(_op, "product_id", "") or "",
                    getattr(_op, "city", "") or "",
                    getattr(_op, "customer_first_name", "") or "",
                    getattr(_op, "short_address_code", "") or "",
                    getattr(_op, "google_maps_url", "") or "",
                    getattr(_op, "address_line", "") or "",
                ))
            )
            if new_state.current_product_focus and not _has_live_order:
                logger.info(
                    "[ORDER FLOW] reset stale current_product_focus after product list display | "
                    "old_focus=%r new_candidates=%d action=%s",
                    _old_focus_title,
                    len(new_state.last_search_candidates),
                    decision.action,
                )
                new_state.current_product_focus = None
            elif new_state.current_product_focus and _has_live_order:
                logger.info(
                    "[ORDER FLOW] preserving current_product_focus during active order | "
                    "focus=%r stage=%s has_product_id=%s has_address=%s has_name=%s "
                    "— order funnel takes priority over list display",
                    _old_focus_title,
                    new_state.stage,
                    bool(getattr(_op, "product_id", "")),
                    bool(getattr(_op, "short_address_code", "") or getattr(_op, "google_maps_url", "")),
                    bool(getattr(_op, "customer_first_name", "")),
                )

        new_state.customer_goal = _infer_customer_goal(intent, decision, state.customer_goal)
        ctx.state = new_state
        suggestion = self._suggestion_engine.suggest(ctx, decision, result)
        new_state.recent_messages = list((history or [])[-20:])
        new_state.conversation_summary = str(
            (sales_context.conversation_memory or {}).get("conversation_summary", "")
            or new_state.conversation_summary
            or ""
        )
        # Only store products that have a valid Salla external_id.
        # Products without external_id can never become orders — keeping them
        # in last_recommended_products causes the stale-focus bug where the
        # customer sees "بنطلون" but the bot replies about "بلوزة غير متوفر".
        def _has_external_id(p: dict) -> bool:
            return bool(str(p.get("external_id") or "").strip())

        if result.data.get("recommended_products"):
            _raw_rec = list(result.data["recommended_products"])
            _filtered_rec = [p for p in _raw_rec if _has_external_id(p)]
            _skipped_rec = len(_raw_rec) - len(_filtered_rec)
            if _skipped_rec:
                logger.warning(
                    "[CATALOG] filtered %d unsynced product(s) from recommended_products "
                    "| tenant=%s skipped_titles=%s",
                    _skipped_rec, ctx.tenant_id,
                    [p.get("title") for p in _raw_rec if not _has_external_id(p)],
                )
            new_state.last_recommended_products = _filtered_rec
        elif sales_context.recommendations:
            _raw_sales = list(sales_context.recommendations[:5])
            _filtered_sales = [p for p in _raw_sales if _has_external_id(p)]
            _skipped_sales = len(_raw_sales) - len(_filtered_sales)
            if _skipped_sales:
                logger.warning(
                    "[CATALOG] filtered %d unsynced product(s) from sales_context.recommendations "
                    "| tenant=%s skipped_titles=%s",
                    _skipped_sales, ctx.tenant_id,
                    [p.get("title") for p in _raw_sales if not _has_external_id(p)],
                )
            new_state.last_recommended_products = _filtered_sales
        new_state.recommended_next_step = suggestion.suggested_next_step
        new_state.pending_action = suggestion.suggested_next_step or new_state.pending_action
        ctx.suggestion = suggestion
        _tenant_tone = ""
        _tenant_overlay = ""
        try:
            from modules.ai.prompts.tenant_overlay import (  # noqa: PLC0415
                get_tenant_tone, load_tenant_ai_overlay,
            )
            _tenant_tone = get_tenant_tone(db, tenant_id)
            _tenant_overlay = load_tenant_ai_overlay(db, tenant_id)
        except Exception:
            pass

        # Slim merchant_context for the prompt — caps product list and FAQ so
        # the LLM payload stays bounded while still covering key facts.
        #
        # Phase 1 prompt-pipeline refactor: we now also surface the raw
        # ``ai_settings`` dict + ``tenant_id`` on the slim context so the
        # new prompt builder can derive Style/Policy/Facts buckets via
        # ``build_tenant_overlay_split``. The legacy ``tenant_overlay``
        # string is still set (backward-compat), but the new builder
        # drops it and re-renders from raw settings — that's what lets
        # the High-Priority banner sit *above* the KB block.
        _ai_settings_for_prompt: Dict[str, Any] = {}
        try:
            from models import TenantSettings  # noqa: PLC0415
            from core.tenant import merge_ai_defaults  # noqa: PLC0415
            _ts = (
                db.query(TenantSettings)
                .filter(TenantSettings.tenant_id == tenant_id)
                .first()
            )
            if _ts:
                _ai_settings_for_prompt = merge_ai_defaults(_ts.ai_settings) or {}
        except Exception:  # noqa: BLE001 — prompt must never break a turn
            _ai_settings_for_prompt = {}

        # Phase 3 — Product/Media resolver overlay for the Brain prompt.
        # The legacy webhook path computes this same overlay just before
        # the LLM call (see whatsapp_webhook.py:3570-3598). We surface
        # it through slim_merchant_ctx so the Brain LLM also learns the
        # ``[PRODUCT:<query>]`` + ``[MEDIA_KEY:<slug>]`` vocabulary and
        # the concrete list of available keys for this tenant. Without
        # this, the High-Priority block hints at the markers but the
        # model never sees the explicit protocol or the keys list.
        _resolver_overlay_text = ""
        try:
            from services import media_resolver as _media_res  # noqa: PLC0415
            from services import media_key_registry as _media_reg  # noqa: PLC0415
            from core.ai_libraries import (  # noqa: PLC0415
                format_resolver_overlay_for_prompt as _fmt_resolver,
            )
            from models import Product as _Product  # noqa: PLC0415

            _keys_avail = _media_res.available_keys_for_tenant(db, tenant_id)
            _keys_block = _media_reg.format_keys_for_prompt(_keys_avail)
            _has_catalog = (
                db.query(_Product.id)
                  .filter(_Product.tenant_id == tenant_id)
                  .limit(1)
                  .first()
                is not None
            )
            _resolver_overlay_text = _fmt_resolver(
                available_media_keys_block=_keys_block,
                catalog_has_products=_has_catalog,
            ) or ""
        except Exception as _ovr_exc:  # noqa: BLE001
            logger.warning(
                "[BrainPipeline] resolver overlay skipped tenant=%s: %s",
                tenant_id, _ovr_exc,
            )
            _resolver_overlay_text = ""

        slim_merchant_ctx: Dict[str, Any] = {}
        if isinstance(ctx.merchant_context, dict) and ctx.merchant_context:
            mc = ctx.merchant_context
            try:
                _faq_approved = list((mc.get("faq") or {}).get("approved") or [])[:5]
                slim_merchant_ctx = {
                    "tenant_id":         tenant_id,
                    "ai_settings":       _ai_settings_for_prompt,
                    "resolver_overlay":  _resolver_overlay_text,
                    "tenant_profile":    mc.get("tenant_profile") or {},
                    "customer":          mc.get("customer") or {},
                    "conversation":      mc.get("conversation") or {},
                    "products":          list((mc.get("products") or []))[:8],
                    "policies":          mc.get("policies") or {},
                    "policy_presence":   mc.get("policy_presence") or {},
                    "brain_profile":     mc.get("brain_profile") or {},
                    "retrieval_rules":   mc.get("retrieval_rules") or {},
                }
                if _faq_approved:
                    slim_merchant_ctx["faq_approved"] = _faq_approved
            except Exception as exc:
                logger.warning(
                    "[BrainPipeline] failed to slim merchant_context "
                    "tenant=%s: %s — sending empty",
                    tenant_id, exc,
                )
                slim_merchant_ctx = {
                    "tenant_id":         tenant_id,
                    "ai_settings":       _ai_settings_for_prompt,
                    "resolver_overlay":  _resolver_overlay_text,
                }

        ctx.reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=state,
            current_state=new_state,
            suggestion=suggestion,
            decision=decision,
            tenant_tone=_tenant_tone,
            tenant_overlay=_tenant_overlay,
            merchant_context=slim_merchant_ctx,
        )

        # ── 7. Compose reply ──────────────────────────────────────────────
        reply: str = await self._composer.compose(decision, result, ctx)

        # ── 7b. Sync candidates with EXACTLY what the composer displayed ──────
        # The composer filters `result.data["products"]` to `safe_products`
        # (only can_checkout=True items) and stores them as `pending_candidates`.
        # If we stored the unfiltered executor list earlier (step 6), the stored
        # candidates may not match the displayed list — causing "1" to resolve
        # to a DIFFERENT product than the one shown. Overwrite with the exact
        # displayed list whenever the composer set pending_candidates.
        _pending_after_compose = result.data.get("pending_candidates")
        if _pending_after_compose:
            _first_before = (
                (new_state.last_search_candidates[0] or {}).get("title")
                if new_state.last_search_candidates else None
            )
            _first_after = (_pending_after_compose[0] or {}).get("title")
            if new_state.last_search_candidates != list(_pending_after_compose):
                logger.warning(
                    "[ORDER FLOW] candidate list corrected after compose | "
                    "before_count=%d first_before=%r after_count=%d first_after=%r "
                    "— stored list now matches displayed list",
                    len(new_state.last_search_candidates), _first_before,
                    len(_pending_after_compose), _first_after,
                )
            # Normalise candidates: every saved entry MUST contain the fields
            # section 3.5 reads to validate the pick.  If can_checkout/orderable
            # are missing, the pick will be rejected even though the product
            # was orderable when displayed.  Default to True for both because
            # the composer only includes products that already passed the
            # can_checkout=True filter (safe_products).
            normalised_candidates = []
            for _c in _pending_after_compose:
                _c = dict(_c)
                _c.setdefault("can_checkout", True)
                _c.setdefault("orderable", _c.get("can_checkout", True))
                normalised_candidates.append(_c)
            new_state.last_search_candidates = normalised_candidates

            # CRITICAL: also clear last_recommended_products. Otherwise
            # section 3.5 of the engine may fall back to those (stale,
            # field-missing) products if last_search_candidates is ever
            # cleared prematurely.
            new_state.last_recommended_products = []

            logger.info(
                "[ORDER FLOW] product list state saved | "
                "last_search_candidates_count=%d first=%r "
                "current_product_focus=%r last_action=%s",
                len(new_state.last_search_candidates),
                (new_state.last_search_candidates[0] or {}).get("title")
                if new_state.last_search_candidates else None,
                (new_state.current_product_focus or {}).get("title"),
                str(decision.action or ""),
            )
            # Verbose dump of stored candidates so we can match them
            # 1:1 with what the customer sees in WhatsApp.
            for _idx, _cand in enumerate(new_state.last_search_candidates[:5], 1):
                logger.info(
                    "[CATALOG] candidate stored | index=%d name=%r "
                    "external_id=%s can_checkout=%s orderable=%s "
                    "stock_qty=%s in_stock=%s status=%s",
                    _idx, _cand.get("title"), _cand.get("external_id"),
                    _cand.get("can_checkout"), _cand.get("orderable"),
                    _cand.get("stock_qty"), _cand.get("in_stock"),
                    _cand.get("status"),
                )

        asked_now = _infer_last_question(decision, result, suggestion)
        if asked_now:
            new_state.last_question_asked = asked_now
            new_state.last_question_answered = False
        else:
            new_state.last_question_asked = state.last_question_asked
            new_state.last_question_answered = True if state.last_question_asked else state.last_question_answered

        # ── 7c. First-contact welcome gate — prepend salaam acknowledgment ─
        # When ``intent.slots["embedded_greeting"]`` is True the customer
        # opened the conversation with a salaam AND an actionable question.
        # The rules layer already routed past ACTION_GREET so the actionable
        # answer is composed in full; here we simply prepend a short warm
        # acknowledgment so the salaam is honoured. After that we mark the
        # conversation as greeted so the welcome card cannot fire later.
        try:
            _embedded = bool(getattr(ctx.intent, "slots", {}).get("embedded_greeting"))
            if (
                _embedded
                and not new_state.greeted
                and decision.action not in {ACTION_GREET, ACTION_SOCIAL_REPLY, ACTION_OUT_OF_SCOPE}
                and isinstance(reply, str)
                and reply.strip()
            ):
                reply = _prepend_first_contact_salaam(reply, ctx)
                new_state.greeted = True
                # Composer must not try to "re-introduce identity" later in
                # the same conversation either.
                new_state.assistant_identity_introduced = True
                result.data["welcome_gate"] = "embedded_greeting_acknowledged"
                logger.info(
                    "[WELCOME_GATE] embedded_greeting acknowledged | tenant=%s "
                    "intent=%s action=%s",
                    tenant_id,
                    getattr(ctx.intent, "name", "?"),
                    decision.action,
                )
        except Exception as _wg_exc:  # noqa: BLE001 — never break a turn
            logger.warning("[WELCOME_GATE] prepend skipped: %s", _wg_exc)

        # ── 8. Persist state ───────────────────────────────────────────────
        self._state_store.save(db, tenant_id, customer_phone, new_state)

        # ── 9. Persist trace ──────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        ctx.state = new_state
        result.data.setdefault("chosen_path", _resolve_chosen_path(decision, result))
        self._memory_updater.update(db, ctx, decision, result, reply, stage_before, latency_ms)
        pending_buttons: List[Dict[str, Any]] = list(result.data.get("pending_buttons") or [])

        # ── 10. Structured turn trace (searchable in Railway logs) ────────
        try:
            logger.info(
                "[BrainTurn] %s",
                json.dumps({
                    "tenant_id":     tenant_id,
                    "phone":         customer_phone[-4:] if len(customer_phone) >= 4 else "****",
                    "turn":          new_state.turn,
                    "message_len":   len(message),
                    # Intent layer
                    "detected_intent": intent.name,
                    "confidence":    round(intent.confidence, 2),
                    "slots":         intent.slots,
                    "method":        intent.extraction_method,
                    # State transition
                    "stage_before":  stage_before,
                    "stage_after":   new_state.stage,
                    "greeted":       new_state.greeted,
                    "product_focus": (new_state.current_product_focus or {}).get("title"),
                    "draft_order":   new_state.draft_order_id,
                    "order_prep_missing": list(getattr(new_state.order_prep, "missing_fields", []) or []),
                    # Commerce facts snapshot
                    "facts": {
                        "products":      facts.product_count,
                        "in_stock":      getattr(facts, "in_stock_count", None),
                        "orderable":     getattr(facts, "orderable", facts.has_products and facts.has_active_integration),
                        "coupons":       facts.has_coupons,
                        "integration":   facts.has_active_integration,
                        "platform":      getattr(facts, "integration_platform", "unknown"),
                        "store":         facts.store_name,
                    },
                    # Decision layer
                    "action":             decision.action,
                    "chosen_path":        result.data.get("chosen_path"),
                    "reason":             decision.reason,
                    "policy_modified":    decision.reason != reason_before_policy,
                    "whether_coupon_logic_considered": suggestion.coupon_logic_considered,
                    "suggested_next_step": suggestion.suggested_next_step,
                    "customer_goal":      new_state.customer_goal,
                    "selected_product":   (new_state.current_product_focus or {}).get("title"),
                    "checkout_city":      getattr(new_state.order_prep, "city", ""),
                    "short_address_code": getattr(new_state.order_prep, "short_address_code", ""),
                    # Execution + response
                    "exec_success":     result.success,
                    "exec_error":       result.error,
                    "response_mode":    "llm" if result.data.get("chosen_path", "").startswith("llm") else "template",
                    "reply_len":        len(reply),
                    "latency_ms":       latency_ms,
                }, ensure_ascii=False),
            )
        except Exception:
            pass   # trace logging must never break the reply path

        # ── Marker scrub at the brain boundary ──────────────────
        #
        # This is the SINGLE chokepoint every downstream consumer
        # (DB persistence via StateManager.save_message + TurnLog +
        # webhook send path + dashboard render) reads from. Scrub
        # here so:
        #
        #   * The MessageEvent row written by StateManager.save_message
        #     never contains `[TEMPLATE:contact_owner]` /
        #     `[TRANSFER]` / `[DEBUG]` / `[ACTION]` / `[INTERNAL]`
        #     — dashboard preview stays clean.
        #   * The wire-layer scrub in
        #     `services.whatsapp_platform.service._scrub_outbound_payload`
        #     becomes a no-op for AI-generated replies (defense in
        #     depth, not load-bearing).
        #   * Downstream string transforms (handoff prefix, CTA
        #     extraction, etc.) never operate on hallucinated
        #     placeholder text.
        #
        # Why brain-boundary AND wire-layer?
        # ----------------------------------
        # The brain boundary catches AI hallucinations at the
        # earliest possible moment in this process. The wire layer
        # catches markers from OTHER outbound paths (manual
        # /conversations/reply, automation engine, orders, cart
        # recovery, admin direct-send) that don't pass through the
        # brain. Two scrubs, two different blast radii — no
        # single path can leak markers to Meta OR the DB.
        #
        # Error policy: scrub is defense-in-depth. If the import
        # or scrub itself fails (e.g. unicode-level regex bug),
        # log and return the un-scrubbed reply — better to send
        # ugly text than fail the whole reply.
        try:
            from core.ai_libraries import scrub_internal_markers  # noqa: PLC0415
            _orig = reply
            reply = scrub_internal_markers(reply or "")
            if reply != _orig:
                logger.info(
                    "[BRAIN_SCRUB] stripped markers from reply "
                    "tenant=%s len_before=%d len_after=%d",
                    tenant_id, len(_orig or ""), len(reply or ""),
                )
        except Exception as _scrub_exc:  # noqa: BLE001
            logger.warning(
                "[BRAIN_SCRUB] failed err=%s — returning original reply",
                _scrub_exc,
            )

        return {
            "reply": reply,
            "buttons": pending_buttons,
            "handoff": decision.action == ACTION_HANDOFF,
        }


# ── Brain state helpers ────────────────────────────────────────────────────────

def _infer_customer_goal(intent: Intent, decision: Decision, previous_goal: str = "") -> str:
    mapping = {
        "who_are_you": "understand_assistant_role",
        "greeting": "start_conversation",
        "ask_product": "discover_products",
        "ask_price": "evaluate_price",
        "start_order": "start_purchase",
        "pay_now": "complete_purchase",
        "ask_shipping": "understand_shipping",
        "ask_store_info": "understand_store_info",
        "ask_owner_contact": "contact_store",
        "ask_payment_info": "share_payment_info",
        "track_order": "track_order",
        "talk_to_human": "reach_human_support",
        "hesitation": "resolve_purchase_hesitation",
    }
    if decision.action == "send_payment_link":
        return "complete_purchase"
    if decision.action == "handoff_to_human":
        return "reach_human_support"
    return mapping.get(intent.name, previous_goal or "general_help")


def _infer_last_question(
    decision: Decision,
    result: ActionResult,
    suggestion: SuggestionSnapshot,
) -> str:
    if decision.action == "clarify":
        return str(result.data.get("question") or "").strip()
    if suggestion.needs_follow_up_question:
        return str(suggestion.follow_up_question or "").strip()
    return ""


def _resolve_chosen_path(decision: Decision, result: ActionResult) -> str:
    chosen = str(result.data.get("chosen_path") or "").strip()
    if chosen:
        return chosen
    if decision.action == "llm_reply":
        return "llm"
    if decision.action in {"greet", "faq_reply", "clarify", "narrow_choices"}:
        return "rule"
    return "action"


def _build_reply_state(
    *,
    ctx: BrainContext,
    previous_state: MerchantConversationState,
    current_state: MerchantConversationState,
    suggestion: SuggestionSnapshot,
    decision: Decision,
    tenant_tone: str = "",
    tenant_overlay: str = "",
    merchant_context: Optional[Dict[str, Any]] = None,
) -> BrainReplyState:
    recent_turns = []
    recent_customer_messages: List[str] = []
    for turn in (ctx.history or [])[-4:]:
        body = str(turn.get("body") or "").strip()
        if not body:
            continue
        role = "customer" if turn.get("direction") == "in" else "assistant"
        recent_turns.append(f"{role}: {body}")
        if role == "customer":
            recent_customer_messages.append(body)

    # ── Semantic stance detection (May 2026 #7) ──────────────────────────
    # Closed-enum classification of the customer's relational frame for THIS
    # turn. Pure function — see modules/ai/brain/intent/stance_detector.py.
    # Returns STANCE_UNKNOWN for anything ambiguous so the rest of the
    # pipeline behaves exactly as before; a non-unknown stance feeds a
    # directive into ``response_goal`` so the LLM reads the message through
    # the right lens (e.g. ``deferred`` ⇒ no sales pitch).
    _stance_result = None
    try:
        from .intent.stance_detector import detect_stance as _detect_stance  # noqa: PLC0415
        _stance_result = _detect_stance(
            ctx.message or "",
            recent_customer_messages=recent_customer_messages[-3:],
            state_hints={
                "greeted": bool(getattr(current_state, "greeted", False)),
                "has_focus_product": bool(current_state.current_product_focus),
            },
        )
        if _stance_result and _stance_result.stance and _stance_result.stance != "unknown":
            logger.info(
                "[STANCE] tenant=%s stance=%s confidence=%.2f evidence=%r "
                "intent=%s preview=%r",
                getattr(ctx, "tenant_id", None),
                _stance_result.stance,
                float(_stance_result.confidence or 0.0),
                (_stance_result.evidence or "")[:80],
                getattr(ctx.intent, "name", "") or "",
                (ctx.message or "")[:80],
            )
    except Exception as _stance_exc:  # noqa: BLE001
        logger.debug(
            "[STANCE] tenant=%s detection failed: %s",
            getattr(ctx, "tenant_id", "?"), _stance_exc,
        )
        _stance_result = None

    sensitivity_score = float(ctx.profile.get("price_sensitivity_score") or 0.5)
    selected_product = current_state.current_product_focus or None

    platform_kb_mode = False
    platform_topic = ""
    platform_kb_excerpt = ""
    if decision.action == ACTION_PLATFORM_REPLY:
        platform_kb_mode = True
        platform_topic = str((decision.args or {}).get("platform_topic") or "general_platform")
        # Prevent JSON state from anchoring the model to a honey SKU when
        # the customer is asking about WABA / Meta / subscription.
        selected_product = None
        try:
            from modules.ai.brain.knowledge_platform_slice import (  # noqa: PLC0415
                extract_platform_kb_excerpt,
            )

            _ai_s = dict(merchant_context or {}).get("ai_settings") or {}
            _raw_kb = str(_ai_s.get("manual_knowledge_base") or "").strip()
            platform_kb_excerpt = extract_platform_kb_excerpt(
                _raw_kb,
                platform_topic,
                ctx.message or "",
            )
            if platform_kb_excerpt:
                logger.info(
                    "[PLATFORM_KB] tenant=%s topic=%s excerpt_chars=%d",
                    ctx.tenant_id,
                    platform_topic,
                    len(platform_kb_excerpt),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PLATFORM_KB] slice failed tenant=%s: %s",
                getattr(ctx, "tenant_id", "?"),
                exc,
            )

    known_facts = {
        "store_name": ctx.facts.store_name,
        "store_url": ctx.facts.store_url,
        "has_products": ctx.facts.has_products,
        "product_count": ctx.facts.product_count,
        "in_stock_count": ctx.facts.in_stock_count,
        "orderable": ctx.facts.orderable,
        "shipping_policy": ctx.facts.shipping_policy,
        "shipping_methods": ctx.facts.shipping_methods,
        "shipping_notes": ctx.facts.shipping_notes,
        "support_hours": ctx.facts.support_hours,
        "contact_phone": ctx.facts.store_contact_phone,
        "contact_email": ctx.facts.store_contact_email,
        "checkout_preparation": current_state.order_prep.to_dict(),
    }

    effective_tone = tenant_tone or str(ctx.profile.get("communication_style") or "neutral")

    return BrainReplyState(
        store_name=ctx.facts.store_name,
        tone=effective_tone,
        stage=current_state.stage,
        customer_goal=current_state.customer_goal,
        identity_already_introduced=bool(
            getattr(current_state, "assistant_identity_introduced", False)
        ),
        selected_product=selected_product,
        price_sensitivity=_price_sensitivity_label(sensitivity_score),
        known_facts=known_facts,
        last_question_asked=previous_state.last_question_asked,
        last_question_answered=previous_state.last_question_answered,
        recommended_next_step=suggestion.suggested_next_step or current_state.recommended_next_step,
        coupon_policy={
            "has_coupons": ctx.facts.has_coupons,
            "eligible_code": ctx.facts.coupon_eligibility,
            "discount_ok_now": suggestion.discount_ok_now,
            "coupon_logic_considered": suggestion.coupon_logic_considered,
        },
        recent_turns=recent_turns,
        policy_reason=str(decision.args.get("policy_reason") or ""),
        conversation_summary=current_state.conversation_summary,
        store_knowledge=(ctx.sales_context.store_profile if ctx.sales_context else {}),
        customer_memory={
            **(ctx.sales_context.customer_profile if ctx.sales_context else {}),
            **(ctx.sales_context.customer_preferences if ctx.sales_context else {}),
        },
        last_recommended_products=list(current_state.last_recommended_products or []),
        tenant_overlay=tenant_overlay,
        explicit_pending_action=current_state.pending_action,
        intent_name=getattr(ctx.intent, "name", "") or "",
        response_goal=_compose_response_goal(
            decision, suggestion, stance=_stance_result,
        ),
        merchant_context=dict(merchant_context or {}),
        platform_kb_mode=platform_kb_mode,
        platform_topic=platform_topic,
        platform_kb_excerpt=platform_kb_excerpt,
        relational_frame=(
            _stance_result.stance if _stance_result
            and _stance_result.stance != "unknown" else ""
        ),
        relational_evidence=(
            _stance_result.evidence if _stance_result
            and _stance_result.stance != "unknown" else ""
        ),
    )


def _compose_response_goal(
    decision: Decision,
    suggestion: SuggestionSnapshot,
    *,
    stance: Any = None,
) -> str:
    """Single-line summary of WHY this turn is being composed.

    Surfaced to the LLM in the operating-rules block so the model knows
    what success looks like instead of inferring it from the message and
    the persona. Kept short so it fits in the prompt without bloating it.

    When ``stance`` is a non-unknown ``StanceResult`` (see
    :mod:`modules.ai.brain.intent.stance_detector`) its directive is
    PREPENDED to the goal — the LLM reads the relational frame BEFORE any
    sales suggestion, so a ``deferred`` / ``polite_close`` / ``objection``
    stance can prevent a tone-deaf pitch. The stance NEVER replaces the
    primary goal; it widens the lens.
    """
    base_goal = _compose_base_response_goal(decision, suggestion)
    return _prepend_stance_directive(base_goal, stance)


def _compose_base_response_goal(decision: Decision, suggestion: SuggestionSnapshot) -> str:
    """Decision-action-specific goal text (no stance, no relational frame).

    Pulled into its own function so the stance enrichment can wrap it
    without re-implementing every branch."""
    if decision.action == ACTION_PLATFORM_REPLY:
        # Critical — keep the commerce-oriented suggestion engine hints from
        # hijacking ``response_goal``. Platform turns are NOT sales funnel.
        return (
            "platform_inquiry — أجيب باختصار ودّي عن منصّة نحلة/الاشتراك/الربط/التقنية "
            "استخداماً لمقطع المعرفة أعلاه فقط؛ ممنوع اقتراح منتجات أو أسعار الكتالوج "
            "أو markers [PRODUCT:/[MEDIA_KEY:"
        )

    # ── Execute-pending offer (May 2026 #5) ───────────────────────────────
    # The decision engine routed a bare confirmation ("اي" / "تمام" /
    # "ي ريت" / "👍") to LLM_REPLY with explicit args carrying the
    # context of the previous offer. Without a strict goal here the LLM
    # would compose a vague "أبشري" / "تمام" reply and never emit a
    # marker — the customer sees a verbal ack but no link / card /
    # image. We tell the model EXACTLY what to do.
    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "execute_pending_offer"
    ):
        _last_q = str((decision.args or {}).get("last_question_asked") or "").strip()
        _pending = str((decision.args or {}).get("pending_action") or "").strip()
        _focus = str((decision.args or {}).get("focus_product") or "").strip()
        lines: List[str] = [
            "execute_pending_offer — العميل ردّ بتأكيد قصير على عرض/سؤال "
            "سابق من الذكاء. نفّذ ما عُرض دون إعادة السؤال ودون رد بكلمة "
            "واحدة فقط مثل «أبشري» أو «تمام»."
        ]
        if _last_q:
            lines.append(f"السؤال السابق من الذكاء: «{_last_q}»")
        if _pending:
            lines.append(f"الإجراء المعلّق (pending_action): {_pending}")
        if _focus:
            lines.append(
                f"المنتج في التركيز: «{_focus}» — استخدم "
                f"`[PRODUCT:{_focus}]` لإرسال البطاقة الفعلية مع الصورة "
                f"والسعر والرابط."
            )
        lines.append(
            "إذا طلب العميل وسيلة دفع/شهادة/باركود استخدم "
            "`[MEDIA_KEY:<slug>]` المناسب من قائمة المفاتيح المتاحة."
        )
        return " | ".join(lines)

    parts: List[str] = []
    if decision.reason:
        parts.append(decision.reason.strip())
    nxt = (suggestion.suggested_next_step or "").strip() if suggestion else ""
    if nxt and nxt not in (decision.reason or ""):
        parts.append(f"next_step={nxt}")
    if suggestion and suggestion.needs_follow_up_question and suggestion.follow_up_question:
        parts.append(f"ask_one={suggestion.follow_up_question.strip()}")
    return " | ".join(parts) or "advance the conversation toward the next sales step"


def _prepend_stance_directive(base_goal: str, stance: Any) -> str:
    """Prepend the relational-frame directive when ``stance`` is meaningful.

    No-op when ``stance`` is ``None`` / ``STANCE_UNKNOWN`` / empty —
    preserves the legacy goal byte-for-byte so paths without stance
    detection (or with ambiguous messages) behave identically.

    The directive table lives in ``stance_detector.STANCE_DIRECTIVES``
    so adding a new stance is a single-table edit; this helper stays
    open/closed.
    """
    if stance is None:
        return base_goal
    s = getattr(stance, "stance", "") or ""
    if not s or s == "unknown":
        return base_goal
    try:
        from .intent.stance_detector import STANCE_DIRECTIVES  # noqa: PLC0415
        directive = STANCE_DIRECTIVES.get(s, "")
    except Exception:
        directive = ""
    if not directive:
        return base_goal
    # Optional evidence note — when present, gives the LLM a hint about
    # which trigger we saw so it can reflect it in the reply ("لاحظت
    # إنك قلت …"). Stays compact via the same separator the rest of
    # the goal uses.
    evidence = getattr(stance, "evidence", "") or ""
    if evidence:
        return f"{directive} | evidence={evidence} | {base_goal}"
    return f"{directive} | {base_goal}"


# Short Gulf-style salaam acknowledgments rotated by message length so
# the prefix never feels canned across a small batch of customers. The
# variants stay under one line each and intentionally do NOT add a
# follow-up question — the actionable answer below already drives the
# conversation forward.
_WELCOME_GATE_PREFIXES: List[str] = [
    "وعليكم السلام ورحمة الله 🌹",
    "وعليكم السلام يا الغالي 🌹",
    "وعليكم السلام 🌷",
    "أهلاً بك 🌹",
    "هلا والله 🌷",
]


def _prepend_first_contact_salaam(reply: str, ctx: Any) -> str:
    """Return ``reply`` with a brief Gulf-style salaam line prepended.

    The prefix is chosen deterministically from the message length so a
    given customer message always produces the same opener (helpful for
    debugging) while different customers see different lines.
    """
    if not isinstance(reply, str) or not reply.strip():
        return reply
    message = str(getattr(ctx, "message", "") or "")
    idx = (len(message) + 7) % len(_WELCOME_GATE_PREFIXES)
    prefix = _WELCOME_GATE_PREFIXES[idx]
    # If the composed reply already opens with a salaam/greeting from the
    # LLM we leave it untouched — double-greeting would feel mechanical.
    head = reply.lstrip()[:30]
    head_l = head.lower()
    if any(
        marker in head
        for marker in (
            "وعليكم السلام",
            "السلام عليكم",
            "أهلاً",
            "أهلا",
            "هلا",
            "حياك",
            "مرحبا",
        )
    ) or head_l.startswith(("hi ", "hello", "hey ")):
        return reply
    return f"{prefix}\n{reply}"


def _history_has_outbound(history: List[Dict[str, Any]]) -> bool:
    """True when at least one prior outbound message exists in history.

    The webhook persists the inbound message BEFORE calling Brain, but
    only outbound rows count as "we already greeted" — an inbound from
    the customer obviously doesn't.
    """
    for turn in (history or []):
        direction = str(turn.get("direction") or "").strip().lower()
        if direction in {"out", "outbound"}:
            body = str(turn.get("body") or "").strip()
            if body:
                return True
    return False


def _price_sensitivity_label(score: float) -> str:
    if score < 0.25:
        return "منخفضة"
    if score < 0.5:
        return "متوسطة"
    if score < 0.75:
        return "مرتفعة"
    return "مرتفعة جداً"


# ── Factory ───────────────────────────────────────────────────────────────────

def build_default_brain() -> MerchantBrain:
    """Wire all Phase 2 default implementations together.

    The decision engine is wrapped (left-to-right) by:
      1. ``DefaultDecisionEngine`` — the rule / state machine engine.
      2. ``PolicyOverrideLayer``   — Phase 1.7, attaches a non-binding
         ``policy_hint`` to ``decision.args``.  No-op when no learned
         policy exists for the current ``(intent, industry)``.
      3. ``PolicyBiasLayer``       — Phase 1.9, narrowly mutates
         ``decision.args`` (UI / choice_count / recommendation_style)
         for non-protected actions.  Hard-disabled by default — the
         master switch ``LEARNED_POLICY_BIAS_ENABLED`` must be flipped
         on for this layer to do anything.

    Both decorators are *defense-in-depth*: failure inside any of them
    falls back to the inner decision unchanged, so wiring them by
    default carries zero behavioral risk for a fresh deploy.
    """
    from .intent.classifier  import DefaultIntentClassifier
    from .state.store        import DefaultStateStore
    from .facts.commerce_facts import DefaultFactsLoader
    from .decision.engine    import DefaultDecisionEngine
    from .decision.policy    import RealPolicyGate
    from .execution.executor import DefaultActionExecutor
    from .compose.responder  import DefaultComposer
    from .memory.updater     import DefaultMemoryUpdater
    from .suggestion.engine  import DefaultSuggestionEngine
    from .facts.sales_context import DefaultSalesContextLoader

    # Build the layered decision engine.  Imports are inside the
    # function so that the brain package can be loaded even when the
    # learning subpackage is absent (e.g. older test fixtures).
    decision_engine: Any = DefaultDecisionEngine()
    try:
        from modules.ai.learning import PolicyBiasLayer, PolicyOverrideLayer
        decision_engine = PolicyOverrideLayer(decision_engine)
        decision_engine = PolicyBiasLayer(decision_engine)
    except Exception:
        # If the learning package fails to import we silently keep the
        # bare engine — the rest of the brain must still work.
        pass

    return MerchantBrain(
        classifier     = DefaultIntentClassifier(),
        state_store    = DefaultStateStore(),
        facts_loader   = DefaultFactsLoader(),
        decision_engine= decision_engine,
        policy_gate    = RealPolicyGate(),    # Phase 2: real rules
        executor       = DefaultActionExecutor(),
        composer       = DefaultComposer(),
        memory_updater = DefaultMemoryUpdater(),
        suggestion_engine = DefaultSuggestionEngine(),
        sales_context_loader = DefaultSalesContextLoader(),
    )


# Module-level singleton — created lazily on first use
_brain_instance: Optional[MerchantBrain] = None


def get_brain() -> MerchantBrain:
    global _brain_instance
    if _brain_instance is None:
        _brain_instance = build_default_brain()
    return _brain_instance
