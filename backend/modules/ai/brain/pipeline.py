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
from datetime import datetime
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
    ACTION_CATALOG_NAVIGATE,
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
# Strip every non-alphanumeric character for fuzzy SKU comparison.
_CATALOG_NORM_RE     = _re_catalog.compile(r"[^a-z0-9]")


def _normalize_sku_token(s: Any) -> str:
    """Lowercase + strip non-alphanumeric chars so SKU variants like
    ``"WA-123"`` / ``"wa_123"`` / ``"wa:123"`` all compare equal."""
    return _CATALOG_NORM_RE.sub("", str(s or "").lower())


# ── Relational layer feature flag (May 2026 — Tenant 33 #49) ─────────
# Default OFF. Per merchant directive: rollout is staged
#   1. flag off (this commit ships)
#   2. flag on for telemetry only — no consumer reads the state
#   3. shadow eval -> Tenant 33 -> gradual.
# Read at the call site so unit tests can flip it via monkeypatch
# without restarting the process.
import os as _os_relational  # noqa: E402


def _relational_layer_enabled() -> bool:
    """True when ``RELATIONAL_LAYER_ENABLED`` env var is set to a
    truthy value (``1`` / ``true`` / ``yes`` / ``on``)."""
    raw = (_os_relational.environ.get("RELATIONAL_LAYER_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _resolve_last_shipment_at(
    *,
    conv_meta: Any,
    history: Any,
) -> Optional[datetime]:
    """Best-effort extraction of the last shipment / dispatch event
    timestamp for the post-purchase-window classifier. Tolerates
    missing / malformed fields and never raises.

    Sources scanned (first match wins):
      * ``conv_meta['shipment']['shipped_at']`` — preferred path.
      * Any inbound history turn whose ``body`` mentions the
        Saudi-post-style "تم شحن" line; we use ``created_at`` of
        that turn.
    """
    try:
        if isinstance(conv_meta, dict):
            shipment = conv_meta.get("shipment") or conv_meta.get("order_shipment") or {}
            if isinstance(shipment, dict):
                stamp = shipment.get("shipped_at") or shipment.get("dispatched_at")
                if isinstance(stamp, datetime):
                    return stamp
                if isinstance(stamp, str) and stamp:
                    try:
                        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    except Exception:  # noqa: silent-ok — timestamp parse fallback
                        pass
    except Exception:  # noqa: silent-ok — history stamp probe must not break pipeline
        pass
    try:
        if isinstance(history, list):
            for turn in reversed(history):
                if not isinstance(turn, dict):
                    continue
                body = str(turn.get("body") or "")
                if "تم شحن" in body or "shipment dispatched" in body.lower():
                    stamp = turn.get("created_at")
                    if isinstance(stamp, datetime):
                        return stamp
                    if isinstance(stamp, str) and stamp:
                        try:
                            return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        except Exception:
                            continue
    except Exception:
        pass
    return None


def _resolve_catalog_product(
    *,
    db: Any,
    tenant_id: int,
    sku: str,
    unit_price: Optional[float],
    allow_price_fallback: bool,
):
    """Best-effort resolution of a catalog-order ``product_retailer_id``
    to a row in the merchant's products table.

    Lookup ladder (stops at the first hit):

        1. ``direct``        — exact match on ``external_id`` /
           ``sku`` / ``meta_retailer_id``.
        2. ``normalized``    — same three columns after stripping
           every non-alphanumeric char and lowercasing both sides.
           Catches Salla / BSP id rewrites that drop hyphens or
           prepend ``"wa-"`` style prefixes.
        3. ``unique_price``  — only when ``allow_price_fallback`` is
           True (no SKU AND no payload-supplied name): if EXACTLY ONE
           tenant product has the same unit price, return it. Two or
           more matches → refuse to guess.
        4. ``miss``          — nothing useful, caller falls back to
           the placeholder pin.

    Returns ``(row_or_None, strategy)``. ``strategy`` is always
    populated for the trace log.
    """
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None, "miss"

    # 1) Direct, indexed match.
    if sku:
        try:
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
                return row, "direct"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CATALOG_FOCUS] direct product lookup failed tenant=%s "
                "sku=%r: %s",
                tenant_id, sku, exc,
            )

    # Both fuzzy strategies need the tenant's product list. Capped so
    # a runaway catalog never balloons the request.
    rows: list = []
    try:
        rows = list(
            db.query(Product)
              .filter(Product.tenant_id == tenant_id)
              .limit(2000)
              .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CATALOG_FOCUS] tenant product fetch failed tenant=%s: %s",
            tenant_id, exc,
        )
        return None, "miss"

    # 2) Normalized SKU match.
    if sku:
        target = _normalize_sku_token(sku)
        if target:
            for r in rows:
                for f in (
                    getattr(r, "external_id", None),
                    getattr(r, "sku", None),
                    getattr(r, "meta_retailer_id", None),
                ):
                    if f and _normalize_sku_token(f) == target:
                        return r, "normalized"

    # 3) Unique-price fallback (only when explicitly allowed).
    if allow_price_fallback and unit_price is not None:
        same_price = []
        for r in rows:
            raw_p = getattr(r, "price", None)
            if raw_p is None:
                continue
            try:
                p = float(raw_p)
            except (TypeError, ValueError):
                continue
            if abs(p - unit_price) < 0.01:
                same_price.append(r)
                if len(same_price) > 1:
                    break  # already non-unique — refuse to guess
        if len(same_price) == 1:
            return same_price[0], "unique_price"
        if len(same_price) > 1:
            logger.info(
                "[CATALOG_FOCUS] price=%s matched %d products — refusing "
                "to guess | tenant=%s",
                unit_price, len(same_price), tenant_id,
            )

    return None, "miss"


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
    # title / numeric id. Unique-price fallback only when WhatsApp itself
    # gave us no name AND the SKU lookup found nothing — we never want
    # to guess on top of a label the BSP already supplied.
    resolved_id: Any = None
    resolved_title: str = ""
    resolved_price: Optional[float] = None
    strategy = "miss"
    try:
        row, strategy = _resolve_catalog_product(
            db=db,
            tenant_id=tenant_id,
            sku=sku,
            unit_price=unit_price,
            allow_price_fallback=not bool(payload_name),
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
            "[CATALOG_FOCUS] product resolution failed tenant=%s sku=%r: %s",
            tenant_id, sku, exc,
        )

    # Title selection priority:
    #   1. Real product row resolved from the merchant's catalog DB
    #      (direct / normalized / unique_price).
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
        "tenant=%s sku=%r db_lookup_matched=%s match_strategy=%s "
        "payload_name=%r resolved_title=%r unit_price=%s currency=%r",
        tenant_id,
        sku,
        bool(resolved_id),
        strategy,
        payload_name,
        resolved_title,
        unit_price,
        currency,
    )


def _maybe_apply_native_catalog_order(
    *,
    db: Any,
    tenant_id: int,
    message: Optional[str],
    state: MerchantConversationState,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Parse WhatsApp native catalog orders into ``order_prep.line_items``."""
    meta = dict(inbound_metadata or {})
    if meta.get("source_type") != "catalog_order":
        return
    if not message:
        return

    logger.info(
        "[WA_NATIVE_ORDER] wa_order_received tenant=%s item_count=%s",
        tenant_id,
        meta.get("item_count"),
    )

    try:
        from core.wa_native_catalog_order import (  # noqa: PLC0415
            apply_native_order_to_state,
            parse_native_catalog_order,
        )

        payload = parse_native_catalog_order(
            {
                "catalog_id": meta.get("catalog_id"),
                "text": meta.get("customer_note"),
                "product_items": meta.get("product_items") or [],
            },
            metadata=meta,
        )
        if not payload.items:
            return

        resolution = apply_native_order_to_state(
            db=db,
            tenant_id=tenant_id,
            state=state,
            payload=payload,
        )
        logger.info(
            "[WA_NATIVE_ORDER] native_order_items_count=%d line_items_matched=%d "
            "line_items_unmatched=%d needs_review_count=%d tenant=%s",
            len(payload.items),
            resolution.matched_count,
            resolution.unmatched_count,
            resolution.needs_review_count,
            tenant_id,
        )

        first = next(
            (
                li
                for li in resolution.line_items
                if li.get("product_id")
            ),
            resolution.line_items[0] if resolution.line_items else None,
        )
        if first:
            state.current_product_focus = {
                "id": first.get("product_id") or first.get("product_retailer_id"),
                "product_retailer_id": first.get("product_retailer_id") or "",
                "title": first.get("product_name") or first.get("title") or "",
                "price": first.get("unit_price") or first.get("price"),
                "currency": first.get("currency") or meta.get("currency") or "",
                "from_catalog_order": True,
                "from_native_catalog_order": True,
                "line_items_count": len(resolution.line_items),
                "is_multi_item": len(resolution.line_items) > 1,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WA_NATIVE_ORDER] apply failed tenant=%s err=%s",
            tenant_id,
            exc,
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
        *,
        human_priority: bool = False,
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

        # ── Conversation quota guard ──────────────────────────────────────────
        from core.wa_usage import check_limit  # noqa: PLC0415

        _quota = check_limit(db, tenant_id, category="service")
        if not _quota.allowed:
            logger.info(
                "[Brain] conversation_limit_exceeded — skipping AI reply | "
                "tenant=%s used=%s limit=%s reason=%s",
                tenant_id,
                _quota.used_total,
                _quota.limit,
                _quota.reason,
            )
            return {
                "reply":   None,
                "skipped": True,
                "reason":  _quota.reason,
                "conversation_quota": {
                    "used":  _quota.used_total,
                    "limit": _quota.limit,
                    "pct":   _quota.pct,
                },
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

        # ── P0 AI disabled kill switch (before intent / arbiter / compose) ──
        try:
            from core.ai_disabled_gate import is_ai_disabled_for_conversation  # noqa: PLC0415

            _ai_disabled = is_ai_disabled_for_conversation(
                db,
                tenant_id=tenant_id,
                customer_phone=customer_phone,
                source="brain_process_entry",
            )
            if _ai_disabled.disabled:
                logger.info(
                    "[AI_DISABLED_GATE] brain_suppressed tenant=%s phone=%s "
                    "conversation_id=%s reason=%s",
                    tenant_id,
                    customer_phone,
                    conversation_id,
                    _ai_disabled.reason,
                )
                return {
                    "reply": None,
                    "skipped": True,
                    "reason": "ai_disabled_gate",
                    "ai_disabled_reason": _ai_disabled.reason,
                }
        except Exception as _brain_gate_exc:  # noqa: BLE001  # noqa: silent-ok
            logger.warning(
                "[AI_DISABLED_GATE] brain entry check failed tenant=%s err=%s",
                tenant_id,
                _brain_gate_exc,
            )

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

        # ── 1b.4 Order context TTL / explicit close ───────────────────────
        try:
            from .commerce.conversation_context_reset import (  # noqa: PLC0415
                maybe_reset_stale_order_context,
            )

            _reset_reason = maybe_reset_stale_order_context(
                state_for_classify,
                message,
            )
            if _reset_reason:
                logger.info(
                    "[ORDER_CONTEXT_RESET] tenant=%s reason=%s preview=%r",
                    tenant_id,
                    _reset_reason,
                    (message or "")[:80],
                )
        except Exception as _ocr_exc:  # noqa: BLE001
            logger.debug(
                "[ORDER_CONTEXT_RESET] skipped tenant=%s err=%s",
                tenant_id,
                _ocr_exc,
            )

        try:
            from .state.product_correction import (  # noqa: PLC0415
                clear_stale_product_state_for_correction,
                detect_product_correction,
            )

            if detect_product_correction(message or ""):
                clear_stale_product_state_for_correction(state_for_classify)
                logger.info(
                    "[PRODUCT_CORRECTION] tenant=%s cleared stale product focus preview=%r",
                    tenant_id,
                    (message or "")[:80],
                )
        except Exception as _pc_exc:  # noqa: BLE001
            logger.debug(
                "[PRODUCT_CORRECTION] skipped tenant=%s err=%s",
                tenant_id,
                _pc_exc,
            )

        # ── 1b.5 Conversation objective (product-origin verification, …) ───
        try:
            from .intent.conversation_objective_guard import (  # noqa: PLC0415
                refresh_conversation_objective,
            )

            _objective_turn = refresh_conversation_objective(
                state_for_classify,
                message,
                profile or {},
            )
            if _objective_turn.active or _objective_turn.cleared:
                logger.info(
                    "[CONVERSATION_OBJECTIVE] tenant=%s active=%s objective=%r "
                    "trigger=%r cleared=%s",
                    tenant_id,
                    _objective_turn.active,
                    _objective_turn.objective,
                    _objective_turn.trigger or "-",
                    _objective_turn.cleared,
                )
        except Exception as _obj_exc:  # noqa: BLE001
            logger.debug(
                "[CONVERSATION_OBJECTIVE] skipped tenant=%s err=%s",
                tenant_id,
                _obj_exc,
            )

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
        _maybe_apply_native_catalog_order(
            db=db,
            tenant_id=tenant_id,
            message=message,
            state=state_for_classify,
            inbound_metadata=(profile or {}).get("inbound_metadata"),
        )
        try:
            from .commerce.status_reply_product_context import (  # noqa: PLC0415
                apply_status_reply_product_context_to_state,
            )

            _sr_meta = dict((profile or {}).get("inbound_metadata") or {})
            _sr_ctx = apply_status_reply_product_context_to_state(
                db=db,
                tenant_id=tenant_id,
                message=message or "",
                state=state_for_classify,
                inbound_metadata=_sr_meta,
            )
            if _sr_ctx is not None and isinstance(profile, dict):
                profile.setdefault("inbound_metadata", {}).update(_sr_meta)
        except Exception as _sr_exc:  # noqa: BLE001  # noqa: silent-ok — status pre-decide must not block pipeline
            logger.debug(
                "[STATUS_REPLY_CTX] pipeline pre-decide hook failed tenant=%s err=%s",
                tenant_id,
                _sr_exc,
            )
        try:
            from core.order_context_prefill import maybe_apply_operational_prefill_to_state  # noqa: PLC0415

            _profile = profile or {}
            maybe_apply_operational_prefill_to_state(
                db,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                customer=_profile.get("customer"),
                phone=customer_phone,
                message=message,
                state=state_for_classify,
                inbound_metadata=_profile.get("inbound_metadata"),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[ORDER_CONTEXT_PREFILL] brain hook failed tenant=%s",
                tenant_id,
            )

        # ── 1a-pre. Commerce conversation guard (P0 drift prevention) ─────
        _commerce_prep = None
        try:
            from .commerce.commerce_conversation_guard import (  # noqa: PLC0415
                prepare_commerce_inbound,
            )

            _commerce_prep = prepare_commerce_inbound(
                message,
                state=state_for_classify,
                history=history,
            )
            if _commerce_prep.message_for_classification:
                message = _commerce_prep.message_for_classification
        except Exception as _ccg_exc:  # noqa: BLE001
            logger.debug(
                "[COMMERCE_CONVERSATION_GUARD] prep failed tenant=%s err=%s",
                tenant_id, _ccg_exc,
            )
            _commerce_prep = None

        # ── 1a. Semantic turn interpreter (Phase 1) ─────────────────────
        # Context-aware repair for short/ambiguous replies BEFORE rigid
        # rule routing. Does not bypass guards — only improves meaning.
        _raw_message = message
        _semantic_interpretation = None
        _classify_message = message
        try:
            from .interpret.semantic_turn_interpreter import (  # noqa: PLC0415
                interpret_semantic_turn,
                log_semantic_turn_interpretation,
            )
            from .interpret.semantic_routing import apply_semantic_intent_override  # noqa: PLC0415

            _semantic_interpretation = interpret_semantic_turn(
                raw_text=message,
                state=state_for_classify,
                history=history,
            )
            if _semantic_interpretation is not None:
                log_semantic_turn_interpretation(
                    tenant_id=tenant_id,
                    interpretation=_semantic_interpretation,
                )
                if _semantic_interpretation.canonical_text:
                    _classify_message = _semantic_interpretation.canonical_text
        except Exception as _sem_exc:  # noqa: BLE001
            logger.debug(
                "[SEMANTIC_TURN_INTERPRETER] skipped tenant=%s err=%s",
                tenant_id, _sem_exc,
            )
            _semantic_interpretation = None
            _classify_message = message

        intent: Intent = await self._classifier.classify(
            _classify_message, history, state_for_classify,
        )
        if _commerce_prep is not None and _commerce_prep.intent_override:
            intent = Intent(
                name=_commerce_prep.intent_override,
                confidence=0.94,
                slots=dict(getattr(intent, "slots", None) or {}),
            )
        if _semantic_interpretation is not None:
            try:
                from .interpret.semantic_routing import apply_semantic_intent_override  # noqa: PLC0415

                intent = apply_semantic_intent_override(
                    intent,
                    _semantic_interpretation,
                    state=state_for_classify,
                )
            except Exception:  # noqa: BLE001
                pass
        if (
            _classify_message
            and (message or "").strip()
            and _classify_message.strip() != (message or "").strip()
        ):
            try:
                from .commerce.solution_seeking import apply_post_repair_suppression  # noqa: PLC0415

                intent = apply_post_repair_suppression(
                    intent,
                    _classify_message,
                    state=state_for_classify,
                    history=history,
                )
            except Exception as _prs_exc:  # noqa: BLE001
                logger.exception(
                    "[POST_REPAIR_SUPPRESSION] apply_post_repair_suppression failed "
                    "tenant=%s: %s",
                    tenant_id,
                    _prs_exc,
                )
        if _raw_message and intent.raw_message != _raw_message:
            intent.raw_message = _raw_message

        # ── 1a.55 Customer Intent Priority Layer (AI-ARCH-007) ─────────
        _intent_priority = None
        try:
            from .intent_priority import (  # noqa: PLC0415
                compute_customer_intent_priority,
                enrich_intent_with_priority,
                log_intent_priority_verdict,
            )

            _intent_priority = compute_customer_intent_priority(
                message=_raw_message or message,
                intent=intent,
                state=state_for_classify,
                profile=profile or {},
            )
            intent = enrich_intent_with_priority(intent, _intent_priority)
            log_intent_priority_verdict(
                tenant_id=tenant_id,
                verdict=_intent_priority,
                preview=_raw_message or message,
                intent_name=intent.name,
            )
        except Exception as _ip_exc:  # noqa: BLE001
            logger.debug(
                "[INTENT_PRIORITY] skipped tenant=%s err=%s",
                tenant_id,
                _ip_exc,
            )
            _intent_priority = None

        # ── 1b. State relevance validation ─────────────────────────────
        _state_relevance = None
        try:
            from .state.state_relevance import (  # noqa: PLC0415
                log_state_relevance,
                validate_state_relevance,
            )

            _state_relevance = validate_state_relevance(
                type("_SRCtx", (), {
                    "message": _raw_message,
                    "state": state_for_classify,
                    "intent": intent,
                    "semantic_interpretation": _semantic_interpretation,
                })(),
                message=_raw_message,
                state=state_for_classify,
                semantic_interpretation=_semantic_interpretation,
            )
            if _state_relevance.active_workflows:
                for _wf in _state_relevance.active_workflows:
                    _rel = True
                    if _wf in ("awaiting_payment_receipt", "payment_flow"):
                        _rel = _state_relevance.payment_state_relevant
                    elif _wf in ("active_fulfillment", "awaiting_location"):
                        _rel = _state_relevance.fulfillment_state_relevant
                    elif _wf == "pending_candidates":
                        _rel = _state_relevance.pending_candidates_relevant
                    log_state_relevance(
                        tenant_id=tenant_id,
                        verdict=_state_relevance,
                        state_name=_wf,
                        relevant=_rel,
                        reason=(
                            "support_listing_topic_shift"
                            if _state_relevance.support_listing_topic_shift and not _rel
                            else (
                                "topic_shift"
                                if _state_relevance.detected_topic_shift and not _rel
                                else "current_turn_match"
                            )
                        ),
                    )
        except Exception as _sr_exc:  # noqa: BLE001
            logger.debug(
                "[STATE_RELEVANCE] skipped tenant=%s err=%s",
                tenant_id, _sr_exc,
            )
            _state_relevance = None

        _browse_defocus = False
        try:
            from .commerce.product_breadth_policy import (  # noqa: PLC0415
                global_availability_browse_requested,
            )

            _browse_defocus = global_availability_browse_requested(
                _raw_message or message or "",
            )
            if _browse_defocus:
                logger.info(
                    "[BROWSE_DEFOCUS] tenant=%s preview=%r — unscoped catalog + KB",
                    tenant_id,
                    (_raw_message or message or "")[:80],
                )
        except Exception:  # noqa: BLE001
            _browse_defocus = False

        _nc_match = None
        try:
            from .intent.non_commerce_classifier import resolve_commerce_block  # noqa: PLC0415
            _nc_match = resolve_commerce_block(
                message,
                inbound_metadata=(profile or {}).get("inbound_metadata"),
                intent_name=intent.name,
                intent_confidence=intent.confidence,
            )
        except Exception:  # noqa: BLE001
            _nc_match = None

        # ── 1a.6 Social & Human Context Layer (P0) ───────────────────────
        _social_human_context = None
        try:
            from .social_human_context import (  # noqa: PLC0415
                compute_social_human_context,
                enrich_intent_with_social_human,
                log_social_human_context,
            )

            _social_human_context = compute_social_human_context(
                message=_raw_message or message,
                intent=intent,
                state=state_for_classify,
                history=history,
                inbound_metadata=(profile or {}).get("inbound_metadata"),
                nc_match=_nc_match,
                intent_priority=_intent_priority,
            )
            intent = enrich_intent_with_social_human(intent, _social_human_context)
            log_social_human_context(
                tenant_id=tenant_id,
                shc=_social_human_context,
                preview=_raw_message or message,
            )
        except Exception as _shc_exc:  # noqa: BLE001
            logger.debug(
                "[SOCIAL_HUMAN_CONTEXT] skipped tenant=%s err=%s",
                tenant_id,
                _shc_exc,
            )
            _social_human_context = None

        from .pre_commerce_gate import (  # noqa: PLC0415
            load_minimal_ai_settings,
            load_minimal_commerce_facts,
            log_pre_commerce_shortcut,
            should_pre_commerce_shortcut,
        )
        _commerce_bundle_early: Dict[str, Any] = {}
        try:
            from core.active_order_context import load_commerce_bundle_from_db  # noqa: PLC0415

            _commerce_bundle_early = load_commerce_bundle_from_db(
                db, tenant_id, customer_phone,
            )
        except Exception:  # noqa: BLE001
            _commerce_bundle_early = {}

        _order_fulfillment_skip = False
        try:
            from .order_context_gate import (  # noqa: PLC0415
                log_order_context_block,
                should_skip_catalog_preload,
            )

            _order_fulfillment_skip = should_skip_catalog_preload(
                message=message or "",
                state=state_for_classify,
                intent=intent,
                commerce_bundle=_commerce_bundle_early,
            )
            if _order_fulfillment_skip:
                log_order_context_block(
                    tenant_id=tenant_id,
                    reason="skip_catalog_preload",
                    preview=(message or "")[:80],
                )
        except Exception:  # noqa: BLE001
            _order_fulfillment_skip = False

        _pre_commerce_shortcut = (
            should_pre_commerce_shortcut(
                intent,
                _nc_match,
                message=message or "",
                state=state_for_classify,
                social_human_context=_social_human_context,
            )
            or _order_fulfillment_skip
        )
        if _pre_commerce_shortcut and not _order_fulfillment_skip:
            log_pre_commerce_shortcut(
                tenant_id=tenant_id,
                intent=intent,
                nc_match=_nc_match,
            )
        elif _pre_commerce_shortcut and _order_fulfillment_skip:
            logger.info(
                "[ORDER_CONTEXT_GATE] tenant=%s skip_catalog_preload=1 "
                "intent=%s preview=%r",
                tenant_id,
                getattr(intent, "name", "?"),
                (message or "")[:80],
            )

        # ── 2. Load state + facts ─────────────────────────────────────────
        state: MerchantConversationState = state_for_classify
        if _pre_commerce_shortcut:
            facts: CommerceFacts = load_minimal_commerce_facts(db, tenant_id)
            sales_context = self._sales_context_loader.load(
                db,
                tenant_id=tenant_id,
                customer_phone=customer_phone,
                state=state,
                history=history,
                profile=profile,
                customer_id=customer_id,
                tenant_context=tenant_ctx,
                pre_commerce_shortcut=True,
            )
            if sales_context is None:
                sales_context = SalesContextSnapshot()
            merchant_context: Dict[str, Any] = {}
            commerce_bundle: Dict[str, Any] = {}
        else:
            facts = self._facts_loader.load(db, tenant_id)

            sales_context = self._sales_context_loader.load(
                db,
                tenant_id=tenant_id,
                customer_phone=customer_phone,
                state=state,
                history=history,
                profile=profile,
                customer_id=customer_id,
                tenant_context=tenant_ctx,
            )

            merchant_context = {}
            try:
                from core.store_knowledge import build_merchant_context  # noqa: PLC0415
                merchant_context = build_merchant_context(
                    db,
                    tenant_id      = tenant_id,
                    customer_phone = customer_phone,
                    product_query  = "" if _browse_defocus else (message or ""),
                    state          = state,
                    history        = history,
                    profile        = profile,
                ) or {}
                if _browse_defocus:
                    merchant_context["browse_defocus"] = True
            except Exception as exc:
                logger.warning(
                    "[BrainPipeline] build_merchant_context failed tenant=%s — "
                    "falling back to legacy context: %s",
                    tenant_id, exc,
                )
                merchant_context = {}

            commerce_bundle = {}
            try:
                from core.active_order_context import load_commerce_bundle_from_db  # noqa: PLC0415

                commerce_bundle = _commerce_bundle_early or load_commerce_bundle_from_db(
                    db, tenant_id, customer_phone,
                )
            except Exception as _cb_exc:  # noqa: BLE001
                logger.exception(
                    "[ACTIVE_ORDER_CONTEXT] pipeline load failed tenant=%s: %s",
                    tenant_id, _cb_exc,
                )

        # ── 3. Assemble context ───────────────────────────────────────────
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
            human_priority = bool(human_priority),
            commerce_bundle  = commerce_bundle,
            block_commerce_escalation=bool(_nc_match),
            non_commerce_category=(
                str(_nc_match.category) if _nc_match else ""
            ),
            semantic_interpretation=_semantic_interpretation,
            raw_message=_raw_message,
            state_relevance=_state_relevance,
            intent_priority=_intent_priority,
            social_human_context=_social_human_context,
        )
        try:
            from .context.fresh_social_context import (  # noqa: PLC0415
                days_since_last_activity,
                log_fresh_social_context,
                should_apply_fresh_social_context,
            )

            _primary_goal = str(
                getattr(_intent_priority, "primary_customer_goal", "") or ""
            )
            _fresh_social, _fresh_social_reason = should_apply_fresh_social_context(
                inbound_text=message or "",
                state=state,
                intent_name=str(getattr(intent, "name", "") or ""),
                primary_customer_goal=_primary_goal,
                inbound_metadata=dict((profile or {}).get("inbound_metadata") or {}),
                human_priority=bool(human_priority),
            )
            ctx.fresh_social_context = _fresh_social
            ctx.fresh_social_context_reason = _fresh_social_reason
            log_fresh_social_context(
                tenant_id=tenant_id,
                phone_tail=(customer_phone or "")[-4:],
                applied=_fresh_social,
                reason=_fresh_social_reason,
                gap_days=days_since_last_activity(state),
            )
        except Exception as _fresh_soc_exc:  # noqa: BLE001  # noqa: silent-ok
            logger.debug(
                "[FRESH_SOCIAL_CONTEXT] evaluate failed tenant=%s err=%s",
                tenant_id,
                _fresh_soc_exc,
            )
        ctx._pre_commerce_shortcut = _pre_commerce_shortcut  # type: ignore[attr-defined]
        try:
            from .commerce.external_outbound_context import apply_external_outbound_context  # noqa: PLC0415

            apply_external_outbound_context(ctx)
        except Exception as _ext_out_exc:  # noqa: BLE001  # noqa: silent-ok — outbound context must not block turn
            logger.debug(
                "[EXTERNAL_OUTBOUND] apply failed tenant=%s err=%s",
                tenant_id,
                _ext_out_exc,
            )
        if (
            _social_human_context is not None
            and _social_human_context.block_commerce_escalation
            and _social_human_context.is_pure_social_turn
        ):
            ctx.block_commerce_escalation = True
        if human_priority:
            logger.info(
                "[HUMAN_PRIORITY] pipeline=enter tenant=%s phone=%s convo=%s — "
                "policy gate will clamp aggressive actions; "
                "composer will append reassurance",
                tenant_id, customer_phone, conversation_id,
            )
        # Attach db for handlers that need it (avoids threading Session issues)
        ctx._db = db  # type: ignore[attr-defined]

        # ── Relational layer (May 2026 — Tenant 33 #49, Commit 1) ────────
        # Behind a feature flag (default OFF). Computes a typed
        # ``RelationalState`` describing the conversation moment, the
        # customer's lifecycle stage, sentiment, post-purchase window
        # and a non-imperative advisory the brain prompt overlay (and
        # later the decision engine + safety nets) can consume. ZERO
        # behaviour change in Commit 1 — no consumer reads
        # ``ctx.relational_state``; this commit only emits the
        # ``[CX]`` telemetry line so we can validate classifier
        # behaviour against production traffic before any layer
        # integration.
        if _relational_layer_enabled():
            try:
                from .relational import (  # noqa: PLC0415
                    compute_relational_state as _compute_relational,
                    log_relational_state as _log_relational,
                )
                _social_category: Optional[str] = None
                _stance_for_relational: Optional[str] = None
                # Light read: stance + social classification are
                # cheap pure functions; we re-run them here so the
                # relational layer is independent of pipeline order.
                try:
                    from .intent.social_classifier import classify_social  # noqa: PLC0415
                    _social_match = classify_social(message or "")
                    if _social_match is not None:
                        _social_category = str(_social_match.category)
                except Exception:
                    _social_category = None
                _last_shipment_at = None
                try:
                    _last_shipment_at = _resolve_last_shipment_at(
                        conv_meta=None,
                        history=history,
                    )
                except Exception:
                    _last_shipment_at = None
                _customer_messages = [
                    str(t.get("body") or "")
                    for t in (history or [])
                    if t.get("direction") == "in"
                ][-3:]
                _handoff_signals: Dict[str, Any] = {}
                try:
                    if intent and getattr(intent, "name", "") == "talk_to_human":
                        _handoff_signals["is_explicit_handoff_request"] = True
                except Exception:
                    pass
                relational = _compute_relational(
                    inbound_text=message or "",
                    intent_name=getattr(intent, "name", "") or "",
                    stance=_stance_for_relational,
                    social_category=_social_category,
                    customer_profile=profile or {},
                    order_state=(
                        state.order_prep.to_dict()
                        if hasattr(state, "order_prep") and state.order_prep
                        else {}
                    ),
                    conversation_summary=(
                        getattr(state, "conversation_summary", None) or {}
                    ),
                    recent_customer_messages=_customer_messages,
                    last_shipment_event_at=_last_shipment_at,
                    handoff_signals=_handoff_signals,
                )
                ctx.relational_state = relational
                _log_relational(
                    tenant_id=tenant_id,
                    phone=customer_phone,
                    state=relational,
                    extra={"intent": getattr(intent, "name", "") or ""},
                )
            except Exception as _rel_exc:  # noqa: BLE001
                logger.exception(
                    "[CX] relational layer compute failed (non-fatal) tenant=%s",
                    tenant_id,
                )

        if merchant_context and not _pre_commerce_shortcut:
            logger.info(
                "[BrainPipeline] merchant_context loaded tenant=%s products=%d "
                "policies=%d has_customer=%s",
                tenant_id,
                len(merchant_context.get("products") or []),
                sum(1 for v in (merchant_context.get("policy_presence") or {}).values() if v),
                bool((merchant_context.get("customer") or {}).get("phone")),
            )

        stage_before = state.stage

        # ── 3.9 Goal-based commerce (P0 — KB retrieval + composition) ────
        try:
            from .types import INTENT_NEED_BASED_PRODUCT_ADVICE  # noqa: PLC0415
            from .commerce.goal.orchestrator import prepare_goal_regimen_bundle  # noqa: PLC0415
            from .commerce.solution_seeking import classify_solution_seeking_commerce  # noqa: PLC0415

            _advisory_turn = (
                intent.name
                in {
                    INTENT_NEED_BASED_PRODUCT_ADVICE,
                    "need_based_product_advice",
                    "solution_seeking_commerce",
                }
                or classify_solution_seeking_commerce(_classify_message or message)
                is not None
            )
            if _advisory_turn and not getattr(ctx, "block_commerce_escalation", False):
                _bundle, _, _kb_hits = prepare_goal_regimen_bundle(
                    db,
                    tenant_id,
                    message,
                    canonical_message=_classify_message,
                )
                ctx.goal_regimen_bundle = _bundle
        except Exception as _gc_exc:  # noqa: BLE001
            logger.debug(
                "[GOAL_COMMERCE] prepare skipped tenant=%s err=%s",
                tenant_id,
                _gc_exc,
            )

        # ── 3.95 Customer identity bridge (B-WIRE-01) ─────────────────────
        # Extract name / contact phone / address hints into order_prep and
        # CRM before the decision engine so replies reflect stored data.
        try:
            from modules.ai.brain.commerce.customer_identity import (  # noqa: PLC0415
                apply_customer_identity_during_order_flow as _apply_customer_identity,
                customer_identity_bridge_enabled as _identity_bridge_on,
            )

            if _identity_bridge_on():
                _apply_customer_identity(ctx, db=db)
        except Exception as _cid_exc:  # noqa: BLE001
            logger.warning(
                "[CUSTOMER_IDENTITY] apply failed tenant=%s err=%s",
                tenant_id,
                _cid_exc,
            )

        # ── 3.96 Pre-decide order extraction (P0 gift-order gate) ───────────
        # Cart + gift recipient + pending location must land BEFORE decide()
        # so order_recovery never sees stale order_prep on the same turn.
        _pre_decide_summary: Dict[str, Any] = {}
        try:
            from modules.ai.brain.commerce.gift_order_gate import (  # noqa: PLC0415
                is_order_shaped_message,
                run_pre_decide_order_extraction,
            )

            if is_order_shaped_message(message or ""):
                _pre_decide_summary = run_pre_decide_order_extraction(
                    ctx,
                    db=db,
                    tenant_id=tenant_id,
                )
                if _pre_decide_summary.get("applied"):
                    logger.info(
                        "[GIFT_ORDER_GATE] pre_decide tenant=%s cart_size=%s "
                        "ready=%s reason=%s",
                        tenant_id,
                        _pre_decide_summary.get("cart_size"),
                        _pre_decide_summary.get("ready_for_order_creation"),
                        _pre_decide_summary.get("ready_reason"),
                    )
        except Exception as _gog_exc:  # noqa: BLE001
            logger.warning(
                "[GIFT_ORDER_GATE] pre_decide failed tenant=%s err=%s",
                tenant_id,
                _gog_exc,
            )

        # ── 3.95 Conversation state isolation (P0 — current intent owns turn) ─
        try:
            from .commerce.conversation_state_isolation import (  # noqa: PLC0415
                maybe_isolate_conversation_on_topic_break,
            )

            maybe_isolate_conversation_on_topic_break(
                message=message or "",
                state=state,
            )
        except Exception as _csi_exc:  # noqa: BLE001  # noqa: silent-ok — isolation must not block decide
            logger.debug(
                "[CONVERSATION_STATE_ISOLATION] pre_decide skipped tenant=%s err=%s",
                tenant_id,
                _csi_exc,
            )

        # ── 4. Decision ───────────────────────────────────────────────────
        try:
            from .commerce.commerce_conversation_guard import (  # noqa: PLC0415
                maybe_lock_honey_order_context,
            )

            maybe_lock_honey_order_context(
                state,
                message or "",
                catalog=list(getattr(facts, "top_products", None) or []),
            )
        except Exception as _hlock_exc:  # noqa: BLE001  # noqa: silent-ok — honey session lock must not block decide
            logger.debug(
                "[COMMERCE_SESSION] honey order lock skipped tenant=%s err=%s",
                tenant_id,
                _hlock_exc,
            )

        try:
            from .commerce.commerce_discovery_shadow import (  # noqa: PLC0415
                trace_commerce_discovery_shadow,
            )

            trace_commerce_discovery_shadow(ctx)
        except Exception as _cds_exc:  # noqa: BLE001
            logger.debug(
                "[COMMERCE_DISCOVERY_SHADOW] trace skipped tenant=%s err=%s",
                tenant_id,
                _cds_exc,
            )

        # ── 3.99 Turn Understanding + Turn Arbiter (Phase 1 shadow / 2A enforce prep)
        try:
            from .turn.shadow import prepare_turn_arbitration  # noqa: PLC0415

            prepare_turn_arbitration(ctx)
        except Exception as _tas_exc:  # noqa: BLE001  # noqa: silent-ok — turn arbiter prep must not block decide
            logger.debug(
                "[TURN_ARBITER_SHADOW] pre_decide skipped tenant=%s err=%s",
                tenant_id,
                _tas_exc,
            )

        # ── 3.991 Merchant Operational Policy (shadow — no enforce / no compose) ─
        try:
            from .policy.shadow import prepare_merchant_operational_policy_shadow  # noqa: PLC0415

            prepare_merchant_operational_policy_shadow(ctx, db=db)
        except Exception as _mop_exc:  # noqa: BLE001  # noqa: silent-ok — shadow must not block decide
            logger.debug(
                "[MERCHANT_OP_POLICY] pre_decide skipped tenant=%s err=%s",
                tenant_id,
                _mop_exc,
            )

        # ── 3.992 Commerce Turn Contract (pre-decide + Phase 2 catalog enforce) ─
        _commerce_turn_contract = None
        _log_commerce_turn_contract_divergence = None
        _enforce_commerce_turn_contract_decision = None
        try:
            from .commerce.commerce_turn_contract import (  # noqa: PLC0415
                attach_commerce_turn_contract,
                build_commerce_turn_contract,
                log_commerce_turn_contract_divergence as _log_ctc_divergence_fn,
                maybe_enforce_commerce_turn_contract_decision,
            )

            _log_commerce_turn_contract_divergence = _log_ctc_divergence_fn
            _enforce_commerce_turn_contract_decision = maybe_enforce_commerce_turn_contract_decision
            _commerce_turn_contract = build_commerce_turn_contract(ctx, db=db)
            attach_commerce_turn_contract(ctx, _commerce_turn_contract)
            logger.info(
                "[COMMERCE_TURN_CONTRACT] pre_decide tenant=%s state=%s goal=%s "
                "forbidden=%s missing=%s catalog_order=%s action_candidate=%s",
                tenant_id,
                _commerce_turn_contract.commerce_state,
                _commerce_turn_contract.next_goal,
                _commerce_turn_contract.forbidden_actions,
                _commerce_turn_contract.missing_fields,
                _commerce_turn_contract.known_facts.get("catalog_order_current_turn"),
                _commerce_turn_contract.action_to_execute,
            )
        except Exception as _ctc_exc:  # noqa: BLE001  # noqa: silent-ok — contract shadow must not block decide
            logger.debug(
                "[COMMERCE_TURN_CONTRACT] pre_decide skipped tenant=%s err=%s",
                tenant_id,
                _ctc_exc,
            )

        try:
            from .turn.ownership import (  # noqa: PLC0415
                attach_conversation_turn_ownership,
                resolve_conversation_turn_ownership,
            )

            _turn_ownership = resolve_conversation_turn_ownership(ctx)
            attach_conversation_turn_ownership(ctx, _turn_ownership)
            logger.info(
                "[TURN_OWNERSHIP] pre_decide tenant=%s owner=%s forbidden=%s explicit_browse=%s",
                tenant_id,
                _turn_ownership.turn_owner,
                sorted(_turn_ownership.forbidden_fallbacks),
                _turn_ownership.explicit_browse_intent,
            )
        except Exception as _to_exc:  # noqa: BLE001  # noqa: silent-ok — ownership must not block decide
            logger.debug(
                "[TURN_OWNERSHIP] pre_decide skipped tenant=%s err=%s",
                tenant_id,
                _to_exc,
            )

        try:
            from .catalog.catalog_browse_turn_policy import (  # noqa: PLC0415
                maybe_suspend_stale_checkout_for_turn,
            )

            maybe_suspend_stale_checkout_for_turn(ctx)
        except Exception as _cbt_exc:  # noqa: BLE001  # noqa: silent-ok — browse isolation must not block decide
            logger.debug(
                "[CATALOG_BROWSE_TURN] pre_decide suspend skipped tenant=%s err=%s",
                tenant_id,
                _cbt_exc,
            )

        decision: Decision   = self._decision_engine.decide(ctx)
        reason_before_policy = decision.reason
        _legacy_decision_for_shadow = decision
        _enforce_result = None

        if _commerce_turn_contract is not None and _log_commerce_turn_contract_divergence:
            try:
                _log_commerce_turn_contract_divergence(
                    _commerce_turn_contract,
                    decision,
                    ctx=ctx,
                    phase="post_decide_raw",
                )
            except Exception as _ctc_div_exc:  # noqa: BLE001  # noqa: silent-ok — divergence log must not block decide
                logger.debug(
                    "[COMMERCE_TURN_CONTRACT] divergence_log skipped tenant=%s err=%s",
                    tenant_id,
                    _ctc_div_exc,
                )

        if _commerce_turn_contract is not None and _enforce_commerce_turn_contract_decision:
            try:
                decision = _enforce_commerce_turn_contract_decision(
                    ctx,
                    _commerce_turn_contract,
                    decision,
                )
            except Exception as _ctc_enf_exc:  # noqa: BLE001  # noqa: silent-ok — contract enforce must not block decide
                logger.debug(
                    "[COMMERCE_TURN_CONTRACT] enforce skipped tenant=%s err=%s",
                    tenant_id,
                    _ctc_enf_exc,
                )

        if str((decision.args or {}).get("topic") or "") == "purchase_channel_selection":
            try:
                from .commerce.checkout_route_owner import persist_checkout_route_state  # noqa: PLC0415

                persist_checkout_route_state(
                    db,
                    tenant_id=tenant_id,
                    phone=customer_phone,
                    awaiting_checkout_channel=True,
                )
            except Exception as _pcs_exc:  # noqa: BLE001  # noqa: silent-ok — channel flag persist must not block decide
                logger.debug(
                    "[PURCHASE_CHANNEL] awaiting flag persist skipped tenant=%s err=%s",
                    tenant_id,
                    _pcs_exc,
                )

        try:
            from .turn.enforce import maybe_enforce_turn_decision  # noqa: PLC0415

            decision, _enforce_result = maybe_enforce_turn_decision(ctx, decision)
        except Exception as _tae_exc:  # noqa: BLE001  # noqa: silent-ok — turn arbiter enforce must not block decide
            logger.debug(
                "[TURN_ARBITER_ENFORCE] skipped tenant=%s err=%s",
                tenant_id,
                _tae_exc,
            )
            _enforce_result = None

        try:
            from .commerce.catalog_order_checkout import (  # noqa: PLC0415
                maybe_enforce_catalog_order_continue_checkout,
            )

            decision = maybe_enforce_catalog_order_continue_checkout(ctx, decision)
        except Exception as _coc_exc:  # noqa: BLE001  # noqa: silent-ok — catalog-order enforce must not block checkout
            logger.debug(
                "[WA_NATIVE_ORDER] continue_checkout_enforce skipped tenant=%s err=%s",
                tenant_id,
                _coc_exc,
            )

        if _commerce_turn_contract is not None and _log_commerce_turn_contract_divergence:
            try:
                _log_commerce_turn_contract_divergence(
                    _commerce_turn_contract,
                    decision,
                    ctx=ctx,
                    phase="post_decide_enforced",
                )
            except Exception as _ctc_post_exc:  # noqa: BLE001  # noqa: silent-ok — divergence log must not block decide
                logger.debug(
                    "[COMMERCE_TURN_CONTRACT] post_enforce divergence skipped tenant=%s err=%s",
                    tenant_id,
                    _ctc_post_exc,
                )

        try:
            from .turn.shadow import complete_turn_shadow_telemetry  # noqa: PLC0415

            complete_turn_shadow_telemetry(
                ctx,
                _legacy_decision_for_shadow,
                enforce_result=_enforce_result,
            )
        except Exception as _tas_post_exc:  # noqa: BLE001  # noqa: silent-ok — turn shadow telemetry must not block decide
            logger.debug(
                "[TURN_ARBITER_SHADOW] post_decide skipped tenant=%s err=%s",
                tenant_id,
                _tas_post_exc,
            )

        try:
            from modules.ai.brain.commerce.gift_order_gate import (  # noqa: PLC0415
                maybe_clear_pending_cart_confirmation,
            )

            maybe_clear_pending_cart_confirmation(
                prep=getattr(state, "order_prep", None),
                decision=decision,
                message=message or "",
                intent_name=str(getattr(intent, "name", "") or ""),
            )
        except Exception as _pcc_clear_exc:  # noqa: BLE001  # noqa: silent-ok — pending cart clear must not block decide
            logger.debug(
                "[GIFT_ORDER_GATE] pending_cart clear skipped tenant=%s err=%s",
                tenant_id,
                _pcc_clear_exc,
            )

        decision             = self._policy_gate.gate(decision, ctx)

        try:
            from .commerce.catalog_search_evidence import (  # noqa: PLC0415
                apply_catalog_search_evidence_gate,
            )

            decision = apply_catalog_search_evidence_gate(ctx, decision)
        except Exception as _csg_exc:  # noqa: BLE001
            logger.exception(
                "[CATALOG_SEARCH_GATE] apply failed tenant=%s err=%s",
                tenant_id,
                _csg_exc,
            )

        # ── 4.5 Relational decision router (May 2026 — Tenant 33 #49,
        # Commit 2). Behind ``RELATIONAL_DECISION_ROUTER_ENABLED``
        # (independent kill switch from the Commit-1 telemetry flag).
        # SOFT preference layer: re-routes a narrow set of (action,
        # moment) pairs (TRACK_ORDER on praise, HANDOFF on complaint,
        # SUGGEST_COUPON on pre-purchase concern). Strictly bound by
        # the architectural rule pinned in
        # ``modules.ai.brain.relational.contracts``: never fabricates
        # business state, never mutates ctx.state, never selects a
        # reply text. Inert if the Commit-1 flag is off (because no
        # ``ctx.relational_state`` was attached).
        try:
            from .relational import (  # noqa: PLC0415
                apply_relational_preference as _apply_relational_pref,
                is_decision_router_enabled as _router_on,
            )
            if _router_on() and getattr(ctx, "relational_state", None) is not None:
                _action_before_router = decision.action
                decision = _apply_relational_pref(decision, ctx)
                if decision.action != _action_before_router:
                    logger.info(
                        "[CX] router rerouted tenant=%s before=%s after=%s",
                        ctx.tenant_id, _action_before_router, decision.action,
                    )
        except Exception as _rr_exc:  # noqa: BLE001  # noqa: silent-ok — relational router optional
            logger.debug(
                "[CX] relational router invocation failed (non-fatal) "
                "tenant=%s err=%s",
                getattr(ctx, "tenant_id", "?"), _rr_exc,
            )

        # ── 4.6 PresentationMode shadow (P1-B Phase 1) ───────────────────
        # Evidence-based mode on Decision.args + [PRESENTATION_MODE] log.
        # Does not change action, compose, or dispatch.
        try:
            from .commerce.presentation_mode import (  # noqa: PLC0415
                apply_presentation_mode_shadow as _apply_presentation_shadow,
            )

            decision = _apply_presentation_shadow(ctx, decision)
        except Exception as _pm_exc:  # noqa: BLE001  # noqa: silent-ok — shadow must never break a turn
            logger.debug(
                "[PRESENTATION_MODE] shadow stamp skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", "?"), _pm_exc,
            )

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

        if (
            decision.action == ACTION_LLM_REPLY
            and (decision.args or {}).get("topic") == "persona_identity"
        ):
            logger.info(
                "[PERSONA_IDENTITY] route=persona_identity intent=%s tenant=%s "
                "pre_commerce_shortcut=%s non_commerce_block=%s",
                ctx.intent.name if ctx.intent else "(none)",
                ctx.tenant_id,
                bool(_pre_commerce_shortcut),
                bool((decision.args or {}).get("block_commerce_escalation")),
            )

        # ── 5. Execute ────────────────────────────────────────────────────
        result: ActionResult = await self._executor.execute(decision, ctx)

        if (
            decision.action == ACTION_LLM_REPLY
            and (decision.args or {}).get("topic") == "persona_identity"
        ):
            result.data["persona_identity_route"] = True
            result.data["pre_commerce_shortcut"] = bool(_pre_commerce_shortcut)
            result.data["non_commerce_block_mode"] = True

        # ── 6. Project next state + suggestion snapshot ───────────────────
        new_state = self._state_store.transition(state, intent, decision)
        # Record the brain action that produced this turn so the
        # `BRAIN_RESULT` log line and `/debug/recent-whatsapp-turns`
        # endpoint can report it without parsing free-form logs.
        new_state.last_action = str(decision.action or "")
        new_state.last_presentation_mode = str(
            (decision.args or {}).get("presentation_mode") or ""
        )

        # Carry conversation objective memory forward (short TTL session lock).
        new_state.active_conversation_objective = str(
            getattr(state, "active_conversation_objective", "") or ""
        )
        new_state.objective_started_turn = int(
            getattr(state, "objective_started_turn", 0) or 0
        )
        new_state.objective_last_reinforced_turn = int(
            getattr(state, "objective_last_reinforced_turn", 0) or 0
        )
        new_state.objective_evidence = dict(getattr(state, "objective_evidence", None) or {})

        try:
            from .commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
                apply_complaint_refund_session_flags,
            )
            from .commerce.post_purchase_feedback_guard import (  # noqa: PLC0415
                apply_post_purchase_feedback_session_flags,
            )
            from .commerce.staff_contact_suppression import (  # noqa: PLC0415
                apply_staff_contact_session_flags,
            )

            apply_complaint_refund_session_flags(
                new_state, ctx.message or "", decision,
            )
            apply_post_purchase_feedback_session_flags(
                new_state,
                ctx.message or "",
                decision,
                history=list(getattr(ctx, "history", None) or []),
            )
            apply_staff_contact_session_flags(
                new_state, ctx.message or "", decision,
            )
        except Exception:  # noqa: BLE001
            pass

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
        _suppress_focus_pin = False
        try:
            from .state.product_information_topic import (  # noqa: PLC0415
                should_suppress_product_focus_pin,
            )

            _suppress_focus_pin = should_suppress_product_focus_pin(ctx.message or "")
        except Exception:  # noqa: BLE001
            _suppress_focus_pin = False
        if decision.action == ACTION_CATALOG_NAVIGATE:
            _suppress_focus_pin = True
        if (
            result.data.get("product")
            and not _suppress_focus_pin
            and decision.action != ACTION_CATALOG_NAVIGATE
            and (
                decision.action == "search_products"
                or decision.action == ACTION_PROPOSE_DRAFT_ORDER
                or not new_state.current_product_focus
            )
        ):
            new_state.current_product_focus = result.data["product"]
            try:
                from .commerce.product_visual import (  # noqa: PLC0415
                    stamp_product_focus_metadata,
                    stamp_visual_focus_metadata,
                )
                stamp_product_focus_metadata(new_state, result.data["product"])
                if intent.name == "product_visual_request":
                    stamp_visual_focus_metadata(new_state, result.data["product"])
            except Exception:  # noqa: BLE001
                pass
        _sel_patch = (decision.args or {}).get("selection_context_patch")
        _coll_patch = (decision.args or {}).get("collection_navigation_patch")
        _nav_patch = (decision.args or {}).get("navigation_state_patch")
        if isinstance(result.data.get("navigation_state_patch"), dict):
            _nav_patch = {**(_nav_patch or {}), **result.data.get("navigation_state_patch")}
        if isinstance(_coll_patch, dict):
            _sel_patch = {**(_sel_patch or {}), **_coll_patch}
        if isinstance(_nav_patch, dict):
            _sel_patch = {**(_sel_patch or {}), **_nav_patch}
            if _nav_patch.get("selection_context_turn") is None:
                _sel_patch["selection_context_turn"] = int(getattr(new_state, "turn", 0) or 0)
        if isinstance(_sel_patch, dict):
            try:
                from .commerce.selection_context import apply_selection_context_patch  # noqa: PLC0415

                apply_selection_context_patch(new_state, _sel_patch)
            except Exception:  # noqa: BLE001  # noqa: silent-ok — selection context patch is best-effort
                logger.debug("[SELECTION_CONTEXT] patch apply failed", exc_info=True)
        _discovery_mode = str((decision.args or {}).get("discovery_mode") or "").strip()
        if _discovery_mode:
            new_state.last_discovery_mode = _discovery_mode
        _variant_binding = result.data.get("variant_binding")
        if isinstance(_variant_binding, dict) and _variant_binding.get("price") is not None:
            new_state.selected_variant = _variant_binding
            _focus = dict(new_state.current_product_focus or {})
            if _focus:
                _focus["price"] = _variant_binding.get("price")
                _focus["variant_id"] = _variant_binding.get("variant_id")
                _focus["variant_label"] = _variant_binding.get("variant_label")
                _focus["unit"] = _variant_binding.get("unit")
                new_state.current_product_focus = _focus
            logger.info(
                "[VARIANT_RESOLUTION_TRACE] tenant=%s state_bound variant_id=%s "
                "variant_label=%r unit=%s price=%s",
                tenant_id,
                _variant_binding.get("variant_id"),
                _variant_binding.get("variant_label"),
                (_variant_binding.get("unit") or {}).get("display_label"),
                _variant_binding.get("price"),
            )
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

        if decision.action == ACTION_PROPOSE_DRAFT_ORDER:
            try:
                from .catalog.navigator_exit import (  # noqa: PLC0415
                    clear_navigator_state_for_order_handoff,
                    is_catalog_navigation_order_handoff_decision,
                )

                if is_catalog_navigation_order_handoff_decision(decision):
                    clear_navigator_state_for_order_handoff(
                        new_state,
                        tenant_id=tenant_id,
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — navigator exit is best-effort
                logger.debug("[CATALOG_NAVIGATOR] order handoff exit failed", exc_info=True)

        try:
            from modules.ai.brain.commerce.cart_state import maybe_apply_cart_message  # noqa: PLC0415

            _cart_changed = False
            _catalog_resolution = None
            if message and new_state.stage in ("ordering", "deciding", "checkout", ""):
                _cart_before = list(getattr(new_state, "cart_items", None) or [])
                maybe_apply_cart_message(
                    state=new_state,
                    prep=new_state.order_prep,
                    message=message,
                    product_info=new_state.current_product_focus,
                )
                try:
                    from core.catalog_authoritative_line_items import (  # noqa: PLC0415
                        filter_authoritative_line_items,
                        sanitize_prep_line_items,
                    )

                    sanitize_prep_line_items(new_state.order_prep)
                    auth_items = filter_authoritative_line_items(
                        list(getattr(new_state, "cart_items", None) or [])
                    )
                    new_state.cart_items = auth_items
                except Exception:  # noqa: BLE001  # noqa: silent-ok — sanitize is best-effort
                    pass
                _cart_after = list(getattr(new_state, "cart_items", None) or [])
                _cart_changed = _cart_before != _cart_after or bool(
                    getattr(new_state.order_prep, "cart_deltas", None)
                )
                if _cart_after and db is not None:
                    from core.wa_cart_catalog_resolver import resolve_and_enrich_cart_state  # noqa: PLC0415
                    _catalog_resolution = resolve_and_enrich_cart_state(
                        db, tenant_id, new_state, new_state.order_prep,
                    )
        except Exception as _cart_exc:  # noqa: BLE001
            logger.debug("[CART_STATE] pipeline hook skipped: %s", _cart_exc)
            _cart_changed = False
            _catalog_resolution = None

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
            _keep_catalog_prep = False
            try:
                from .commerce.catalog_order_checkout import (  # noqa: PLC0415
                    is_catalog_line_items_authoritative_from_prep,
                )

                _keep_catalog_prep = is_catalog_line_items_authoritative_from_prep(
                    getattr(new_state, "order_prep", None),
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog guard is best-effort
                _keep_catalog_prep = False
            if _keep_catalog_prep:
                logger.warning(
                    "[ORDER FLOW] product_unsyncable ignored — catalog line items authoritative | "
                    "previous_focus=%s",
                    (new_state.current_product_focus or {}).get("title"),
                )
            else:
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
        try:
            from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
                filter_products_for_browse_turn,
            )

            _search_products = filter_products_for_browse_turn(
                list(_search_products),
                message=ctx.message or "",
                query=str(
                    result.data.get("query")
                    or (decision.args or {}).get("query")
                    or ""
                ),
                source=str((decision.args or {}).get("source") or "").strip().lower(),
                last_browse_query=str(getattr(new_state, "last_browse_query", "") or ""),
                state=new_state,
            )
            if result.data.get("products"):
                result.data["products"] = list(_search_products)
        except Exception:  # noqa: BLE001
            logger.exception("[BROWSE_CATEGORY_GUARD] pipeline product filter failed")
        _breadth_cap = 16
        _pb = result.data.get("product_breadth") or {}
        if _pb.get("policy_enabled", True) and _pb.get("display_limit"):
            _breadth_cap = max(1, int(_pb["display_limit"]))

        if decision.args.get("rejected_product"):
            # Customer picked a product that was not orderable. The decision
            # engine routed to ACTION_SEARCH_PRODUCTS with alternatives.
            # Clear product focus so we don't loop on the rejected product,
            # and replace candidates with the orderable alternatives.
            new_state.current_product_focus = None
            alts = (
                result.data.get("pending_candidates")
                or decision.args.get("alternatives")
                or _search_products
            )
            new_state.last_search_candidates = list(alts)[:_breadth_cap]
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
        elif str(result.data.get("discovery_output_kind") or "").strip().lower() == "collections":
            _collections = list(result.data.get("collections") or [])
            new_state.last_search_candidates = []
            try:
                from .commerce.selection_context import stamp_selection_context_from_products  # noqa: PLC0415

                stamp_selection_context_from_products(
                    new_state,
                    products=[],
                    collections=_collections,
                    discovery_mode=_discovery_mode or str(
                        getattr(new_state, "last_discovery_mode", "") or ""
                    ),
                    selected_collection="",
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — selection context stamp is best-effort
                logger.debug("[SELECTION_CONTEXT] collections stamp failed", exc_info=True)
            logger.info(
                "[ORDER FLOW] collection list displayed | collections=%d action=%s",
                len(_collections),
                decision.action,
            )
        elif (
            str(getattr(new_state, "catalog_navigation_source", "") or "").strip() == "group_products"
            or (
                decision.action == ACTION_CATALOG_NAVIGATE
                and str(result.data.get("chosen_path") or "") == "catalog_navigation_group_products"
            )
        ):
            try:
                from .catalog.numeric_ownership import sync_group_products_single_source  # noqa: PLC0415

                _gp_page = sync_group_products_single_source(new_state)
                logger.info(
                    "[NUMERIC_OWNERSHIP] group_products_single_source tenant=%s "
                    "presented_count=%d action=%s",
                    tenant_id,
                    len(_gp_page),
                    decision.action,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — sync is best-effort
                logger.debug("[NUMERIC_OWNERSHIP] group_products sync failed", exc_info=True)
        elif _search_products:
            # Must match numbered list shown to the customer (breadth policy).
            new_state.last_search_candidates = list(_search_products)[:_breadth_cap]
            try:
                from .commerce.selection_context import stamp_selection_context_from_products  # noqa: PLC0415

                stamp_selection_context_from_products(
                    new_state,
                    products=new_state.last_search_candidates,
                    collections=result.data.get("collections"),
                    discovery_mode=_discovery_mode or str(
                        getattr(new_state, "last_discovery_mode", "") or ""
                    ),
                    selected_collection=str(
                        (new_state.commerce_session or {}).get("active_category")
                        or getattr(new_state, "selected_collection", "")
                        or ""
                    ),
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — selection context stamp is best-effort
                logger.debug("[SELECTION_CONTEXT] stamp failed after product list", exc_info=True)

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
        try:
            new_state.last_inbound_canonical = str(_classify_message or message or "")
            new_state.last_inbound_canonical_turn = int(state.turn or 0)
        except Exception:  # noqa: BLE001
            pass
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
        _structured_facts_block: str = ""
        _structured_behavior_block: str = ""
        _resolver_overlay_text = ""

        if _pre_commerce_shortcut:
            _ai_settings_for_prompt = load_minimal_ai_settings(db, tenant_id)
        else:
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

            # Smart Store Knowledge Hub (Phase 1+):
            # Pre-bake the structured facts block when the merchant has rows
            # in ``merchant_knowledge_sections``. This is computed here
            # (where ``db`` is in scope) and passed through merchant_context
            # so the IO-free prompt_builder can swap it in for the legacy
            # ``manual_knowledge_base`` text. Empty string → prompt_builder
            # falls back to the legacy overlay path.
            #
            # Phase 3 — Product scoping
            # ─────────────────────────
            # Collect the catalog product ids the conversation is currently
            # "about" — the active focus + recently recommended products —
            # so the overlay can suppress product-scoped sections that
            # belong to OTHER products. We pass ``None`` (not an empty set)
            # when we don't have any signal yet so day-1 deployments keep
            # showing every product extra.
            _active_pids: Optional[set] = None
            try:
                from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
                    resolve_kb_active_product_ids,
                )

                _active_pids = resolve_kb_active_product_ids(
                    new_state,
                    _raw_message or message or "",
                )
            except Exception:  # noqa: BLE001
                _active_pids = None

            try:
                from modules.ai.prompts.tenant_overlay import (  # noqa: PLC0415
                    build_behavioral_overlay_block,
                    build_structured_facts_block,
                )
                _structured_facts_block = build_structured_facts_block(
                    db, tenant_id, active_product_ids=_active_pids,
                ) or ""
                _structured_behavior_block = build_behavioral_overlay_block(
                    db, tenant_id,
                ) or ""
            except Exception as _kb_exc:  # noqa: BLE001
                logger.warning(
                    "[BrainPipeline] structured KB build failed tenant=%s: %s",
                    tenant_id, _kb_exc,
                )
                _structured_facts_block = ""
                _structured_behavior_block = ""

            # Phase 3 — Product/Media resolver overlay for the Brain prompt.
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
        if _pre_commerce_shortcut:
            slim_merchant_ctx = {
                "tenant_id": tenant_id,
                "ai_settings": _ai_settings_for_prompt,
            }
            if _order_fulfillment_skip:
                slim_merchant_ctx["order_fulfillment_update"] = True
            elif should_pre_commerce_shortcut(intent, _nc_match):
                slim_merchant_ctx["pre_commerce_social"] = True
        elif isinstance(ctx.merchant_context, dict) and ctx.merchant_context:
            mc = ctx.merchant_context
            try:
                _faq_approved = list((mc.get("faq") or {}).get("approved") or [])[:5]
                slim_merchant_ctx = {
                    "tenant_id":         tenant_id,
                    "ai_settings":       _ai_settings_for_prompt,
                    "resolver_overlay":  _resolver_overlay_text,
                    "structured_facts_block": _structured_facts_block,
                    "structured_behavior_block": _structured_behavior_block,
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
                    "structured_facts_block": _structured_facts_block,
                    "structured_behavior_block": _structured_behavior_block,
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
            db=db,
        )

        # ── 6.98 Final Turn Contract (Phase 3.1 shadow) ────────────────────
        _final_turn_contract = None
        try:
            from .turn.final_turn_contract import (  # noqa: PLC0415
                attach_final_turn_contract,
                build_final_turn_contract,
            )
            from .turn.flags import is_final_turn_contract_shadow_enabled  # noqa: PLC0415

            if is_final_turn_contract_shadow_enabled():
                _final_turn_contract = build_final_turn_contract(ctx, decision, result)
                attach_final_turn_contract(ctx, _final_turn_contract)
        except Exception as _ftc_exc:  # noqa: BLE001  # noqa: silent-ok — shadow contract must not block compose
            logger.debug(
                "[FINAL_TURN_CONTRACT] build skipped tenant=%s err=%s",
                tenant_id,
                _ftc_exc,
            )

        # ── 6.99 OwnerBrief native compose (Phase 3A) ─────────────────────
        _owner_brief_attached = False
        try:
            from .turn.compose_bridge import maybe_attach_owner_brief_for_compose  # noqa: PLC0415

            decision, _owner_brief_attached = maybe_attach_owner_brief_for_compose(
                decision, ctx,
            )
            if _owner_brief_attached:
                logger.info(
                    "[TURN_OWNER_BRIEF_COMPOSE] tenant=%s attached=true "
                    "owner=%s compose_mode=%s",
                    tenant_id,
                    (decision.args or {}).get("turn_owner"),
                    (decision.args or {}).get("compose_mode"),
                )
        except Exception as _obc_exc:  # noqa: BLE001  # noqa: silent-ok — brief attach must not block compose
            logger.debug(
                "[TURN_OWNER_BRIEF_COMPOSE] skipped tenant=%s err=%s",
                tenant_id,
                _obc_exc,
            )

        # ── 7. Compose reply ──────────────────────────────────────────────
        reply: str = await self._composer.compose(decision, result, ctx)

        if _final_turn_contract is not None:
            try:
                from .turn.final_turn_audit import audit_final_turn_reply  # noqa: PLC0415

                _ftc_post_compose = audit_final_turn_reply(
                    _final_turn_contract,
                    reply or "",
                    phase="post_compose",
                    tenant_id=tenant_id,
                    result_data=dict(getattr(result, "data", None) or {}),
                )
                if _ftc_post_compose.has_violations:
                    result.data["final_turn_violations_post_compose"] = list(
                        _ftc_post_compose.violations
                    )
            except Exception as _ftc_audit_exc:  # noqa: BLE001  # noqa: silent-ok — shadow audit must not block reply
                logger.debug(
                    "[FINAL_TURN_VIOLATION] post_compose audit skipped tenant=%s err=%s",
                    tenant_id,
                    _ftc_audit_exc,
                )

        try:
            from .turn.observability import log_turn_outcome  # noqa: PLC0415

            _telemetry = getattr(ctx, "turn_shadow_telemetry", None)
            _enforced = bool(getattr(getattr(ctx, "turn_enforce_result", None), "enforced", False))
            if _telemetry is not None:
                log_turn_outcome(
                    logger,
                    tenant_id=tenant_id,
                    telemetry=_telemetry,
                    enforced=_enforced,
                    compose_used_brief=_owner_brief_attached or bool(
                        (decision.args or {}).get("owner_brief")
                    ),
                    reply_preview=reply or "",
                )
        except Exception as _outcome_exc:  # noqa: BLE001  # noqa: silent-ok — outcome log must not block reply
            logger.debug(
                "[TURN_ARBITER_OUTCOME] skipped tenant=%s err=%s",
                tenant_id,
                _outcome_exc,
            )

        try:
            from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: PLC0415
            from .commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
                should_block_order_draft_injection,
            )

            if not should_block_order_draft_injection(
                brain_state=new_state,
                customer_message=ctx.message or "",
                decision=decision,
                history=list(getattr(ctx, "history", None) or []),
            ):
                reply = maybe_inject_draft_flow_reply(
                    reply=reply or "",
                    order_prep=new_state.order_prep,
                    brain_state=new_state,
                    catalog_resolution=_catalog_resolution,
                    cart_changed=_cart_changed,
                    customer_message=ctx.message or "",
                    history=list(getattr(ctx, "history", None) or []),
                )
            if reply and result.data.get("wa_draft_reply_injected") is None:
                result.data["wa_draft_reply_injected"] = bool(_cart_changed or _catalog_resolution)
        except Exception as _draft_reply_exc:  # noqa: BLE001
            logger.warning(
                "[WA_DRAFT_CONFIRM] pipeline hook failed tenant=%s err=%s",
                tenant_id, _draft_reply_exc,
            )

        try:
            from .commerce.product_visual import stamp_visual_focus_from_outbound_reply  # noqa: PLC0415
            stamp_visual_focus_from_outbound_reply(new_state, reply)
        except Exception:  # noqa: BLE001
            pass

        # ── 7a. Human-Priority reassurance suffix ─────────────────────────
        # When the turn is being handled under Human-Priority Mode (the
        # customer asked for a human and the team hasn't picked up yet),
        # we append a SHORT reassurance line so the customer never feels
        # the AI is trying to win the conversation back. The line is
        # appended ONLY when:
        #   * ``ctx.human_priority`` is True, AND
        #   * the composer actually produced text (no point appending
        #     to an empty reply), AND
        #   * the composed text doesn't already mention "موظف" /
        #     "الفريق" / "إنسان" — the brain may have already
        #     reassured organically (e.g. handoff acknowledgement)
        #     and double-reassurance is awkward.
        if getattr(ctx, "human_priority", False) and reply:
            already_reassures = any(
                kw in reply for kw in ("موظف", "الفريق", "إنسان", "نتواصل", "نرد قريب")
            )
            if not already_reassures:
                reassurance = "\n\n🌿 فريقنا وصلته رسالتك وراح يرد عليك قريب."
                logger.info(
                    "[HUMAN_PRIORITY] suffix appended tenant=%s phone=%s "
                    "reply_len_in=%d action=%s",
                    ctx.tenant_id, (ctx.customer_phone or "")[-4:],
                    len(reply), decision.action,
                )
                reply = reply + reassurance
            else:
                logger.info(
                    "[HUMAN_PRIORITY] suffix skipped tenant=%s phone=%s "
                    "reason=already_reassures action=%s",
                    ctx.tenant_id, (ctx.customer_phone or "")[-4:], decision.action,
                )

        # ── 7b. Sync candidates with EXACTLY what the composer displayed ──────
        # The composer filters `result.data["products"]` to `safe_products`
        # (only can_checkout=True items) and stores them as `pending_candidates`.
        # If we stored the unfiltered executor list earlier (step 6), the stored
        # candidates may not match the displayed list — causing "1" to resolve
        # to a DIFFERENT product than the one shown. Overwrite with the exact
        # displayed list whenever the composer set pending_candidates.
        _pending_after_compose = result.data.get("pending_candidates")
        if _pending_after_compose and str(
            getattr(new_state, "catalog_navigation_source", "") or ""
        ).strip() != "group_products":
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
        elif (
            _pending_after_compose
            and str(getattr(new_state, "catalog_navigation_source", "") or "").strip() == "group_products"
        ):
            try:
                from .catalog.numeric_ownership import sync_group_products_single_source  # noqa: PLC0415

                sync_group_products_single_source(new_state)
                logger.info(
                    "[NUMERIC_OWNERSHIP] skipped compose candidate overwrite tenant=%s "
                    "reason=group_products_single_source",
                    tenant_id,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — sync is best-effort
                logger.debug("[NUMERIC_OWNERSHIP] post-compose sync failed", exc_info=True)

        # ── 7b-2. Progressive browse pool for "باقي الخيارات" ─────────────
        _src = str((decision.args or {}).get("source") or "").strip().lower()
        _browse_pool = result.data.get("browse_pool")
        _browse_offset = result.data.get("browse_offset")
        if _browse_pool is not None and _src not in {"replay"}:
            try:
                from .commerce.commerce_browse_category_guard import (  # noqa: PLC0415
                    filter_products_for_browse_turn,
                )

                _browse_pool = filter_products_for_browse_turn(
                    list(_browse_pool),
                    message=ctx.message or "",
                    query=str(
                        result.data.get("query")
                        or (decision.args or {}).get("query")
                        or ""
                    ),
                    source=_src,
                    last_browse_query=str(getattr(new_state, "last_browse_query", "") or ""),
                    state=new_state,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[BROWSE_CATEGORY_GUARD] browse_pool filter failed")
            new_state.catalog_browse_pool = list(_browse_pool)
        if _browse_offset is not None and _src == "show_more":
            new_state.catalog_browse_offset = int(_browse_offset)
        elif _pending_after_compose and _src not in {"replay", "show_more"}:
            new_state.catalog_browse_offset = len(_pending_after_compose)
        if str(result.data.get("query") or (decision.args or {}).get("query") or "").strip():
            new_state.last_browse_query = str(
                result.data.get("query") or (decision.args or {}).get("query") or ""
            ).strip()

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
            _shc = getattr(ctx, "social_human_context", None)
            _suppress_prepend = bool(
                _shc is not None
                and getattr(_shc, "suppress_embedded_greeting_prepend", False)
            )
            if (
                _embedded
                and not _suppress_prepend
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

        # ── 9b. Marker scrub at the brain boundary ─────────────────
        #
        # Run scrub BEFORE the alignment check + structured trace
        # log so the validator + log see the SAME text the
        # downstream consumers (DB row, webhook, dashboard) receive.
        # This is the single chokepoint that strips internal
        # planner / debug / action markers — see
        # ``core.ai_libraries.scrub_internal_markers``.
        try:
            from core.ai_libraries import scrub_internal_markers  # noqa: PLC0415
            _orig_scrub = reply
            reply = scrub_internal_markers(reply or "")
            if reply != _orig_scrub:
                logger.info(
                    "[BRAIN_SCRUB] stripped markers from reply "
                    "tenant=%s len_before=%d len_after=%d",
                    tenant_id, len(_orig_scrub or ""), len(reply or ""),
                )
        except Exception as _scrub_exc:  # noqa: BLE001
            logger.warning(
                "[BRAIN_SCRUB] failed err=%s — returning original reply",
                _scrub_exc,
            )

        try:
            from core.outbound_sanitizer import sanitize_outbound_text  # noqa: PLC0415
            _orig_policy = reply
            reply, _policy_hit = sanitize_outbound_text(
                reply or "",
                tenant_id=tenant_id,
                recipient=customer_phone,
            )
            if _policy_hit:
                logger.info(
                    "[BRAIN_SCRUB] internal policy leak scrubbed tenant=%s "
                    "len_before=%d len_after=%d",
                    tenant_id, len(_orig_policy or ""), len(reply or ""),
                )
        except Exception as _policy_exc:  # noqa: BLE001  # noqa: silent-ok — policy scrub must never block reply
            logger.debug(
                "[BRAIN_SCRUB] policy scrub skipped tenant=%s: %s",
                tenant_id, _policy_exc,
            )

        # ── 10a. Single per-turn audit fields (May 2026 #12) ──────────────
        #
        # Compute the answer-alignment outcome BEFORE the structured
        # log so it can ship in the same line. Default mode is
        # log-only — see ``postprocess.answer_alignment.regen_enabled``
        # for the opt-in env flag. Wrapped in try/except so a
        # validator bug never breaks a turn.
        _align_passed: bool = True
        _align_mismatch: str = ""
        _align_regen_fired: bool = False
        try:
            from modules.ai.brain.postprocess.answer_alignment import (  # noqa: PLC0415
                check_alignment, regen_enabled, emit_mismatch_log,
            )
            _align_result = check_alignment(
                last_user_message=message or "",
                reply=reply or "",
                intent_name=getattr(intent, "name", "") or "",
                action=getattr(decision, "action", "") or "",
                order_status=str(getattr(new_state.order_prep, "order_status", "") or ""),
                awaiting_payment_receipt=bool(
                    getattr(new_state.order_prep, "awaiting_payment_receipt", False)
                ),
            )
            _align_passed = _align_result.passed
            _align_mismatch = _align_result.mismatch_type
            if not _align_result.passed:
                _align_regen_fired = regen_enabled()
                emit_mismatch_log(
                    tenant_id=tenant_id,
                    phone=customer_phone or "",
                    turn=getattr(new_state, "turn", 0),
                    last_user_message=message or "",
                    reply=reply or "",
                    result=_align_result,
                    intent_name=getattr(intent, "name", "") or "",
                    action=getattr(decision, "action", "") or "",
                    order_status=str(getattr(new_state.order_prep, "order_status", "") or ""),
                    awaiting_payment_receipt=bool(
                        getattr(new_state.order_prep, "awaiting_payment_receipt", False)
                    ),
                    regen_will_fire=_align_regen_fired,
                )
                # ── AI Quality Monitor — append-only DB row ─────
                # Mirrors the log line into ``ai_quality_events``
                # so the admin dashboard can browse misclassifications
                # without grepping Railway. Never raises into the
                # turn — persistence is best-effort observability.
                try:
                    from core.ai_quality_events import (  # noqa: PLC0415
                        persist_alignment_mismatch,
                    )
                    _slots = getattr(intent, "slots", None) or {}
                    _social_cat = str(_slots.get("social_category") or "")
                    _chosen_path_for_q = str(result.data.get("chosen_path") or "")
                    _model_for_q = str(
                        result.data.get("model_used")
                        or result.data.get("llm_model")
                        or ""
                    )
                    _fb_used = bool(
                        "fallback" in _chosen_path_for_q
                        or "timeout" in _chosen_path_for_q
                        or "duplicate" in _chosen_path_for_q
                    )
                    persist_alignment_mismatch(
                        db,
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        customer_phone=customer_phone or "",
                        inbound_text=message or "",
                        reply_text=reply or "",
                        mismatch_type=_align_result.mismatch_type,
                        mismatch_reason=_align_result.reason,
                        detected_intent=getattr(intent, "name", "") or "",
                        social_category=_social_cat,
                        action_taken=getattr(decision, "action", "") or "",
                        chosen_path=_chosen_path_for_q,
                        fallback_used=_fb_used,
                        order_status=str(
                            getattr(new_state.order_prep, "order_status", "") or ""
                        ),
                        awaiting_payment_receipt=bool(
                            getattr(new_state.order_prep, "awaiting_payment_receipt", False)
                        ),
                        model_used=_model_for_q,
                        turn=getattr(new_state, "turn", 0),
                        alignment_passed=False,
                        regen_fired=_align_regen_fired,
                    )
                except Exception as _persist_exc:  # noqa: BLE001
                    logger.warning(
                        "[AI_QUALITY] persistence skipped err=%s",
                        _persist_exc,
                    )
                if _align_regen_fired:
                    logger.info(
                        "[ALIGN_MISMATCH] regen requested — clearing "
                        "reply for downstream rebuild | tenant=%s "
                        "mismatch=%s",
                        tenant_id, _align_result.mismatch_type,
                    )
                    reply = ""
        except Exception as _align_exc:  # noqa: BLE001
            logger.warning(
                "[ALIGN_MISMATCH] post-compose check failed err=%s — "
                "returning reply unchanged",
                _align_exc,
            )

        _chosen_path = str(result.data.get("chosen_path") or "")
        _navigator_owner_locked = bool(
            result.data.get("owner_locked")
            and str(result.data.get("turn_owner") or "") == "catalog_navigation"
        )
        _owned_reply_snapshot = reply or ""
        _owned_path_snapshot = _chosen_path
        _owned_kind_snapshot = str(result.data.get("discovery_output_kind") or "")
        _owned_hash_snapshot = str(result.data.get("owner_reply_hash") or "")
        _fallback_used = bool(
            "fallback" in _chosen_path
            or "timeout" in _chosen_path
            or "duplicate" in _chosen_path
        )
        _social_category = ""
        try:
            slots = getattr(intent, "slots", None) or {}
            _social_category = str(slots.get("social_category") or "")
        except Exception:
            pass
        _model_used = str(
            result.data.get("model_used")
            or result.data.get("llm_model")
            or ""
        )

        _guard_replaced: dict[str, bool] = {}

        try:
            from modules.ai.brain.postprocess.payment_reply_guard import (  # noqa: PLC0415
                apply_payment_reply_guard,
            )
            _prg_meta = dict((profile or {}).get("inbound_metadata") or {})
            _op = getattr(new_state, "order_prep", None)
            if _op is not None:
                _focus = getattr(new_state, "current_product_focus", None)
                _prg_meta["awaiting_payment_receipt"] = bool(
                    getattr(_op, "awaiting_payment_receipt", False)
                )
                _prg_meta["payment_receipt_received"] = bool(
                    getattr(_op, "payment_receipt_received", False)
                )
                _prg_meta["order_status"] = str(getattr(_op, "order_status", "") or "")
                _prg_meta["payment_method"] = str(
                    getattr(_op, "payment_method", "") or ""
                )
                if _focus is not None:
                    _prg_meta["selected_product"] = str(
                        getattr(_focus, "title", None)
                        or getattr(_focus, "name", None)
                        or ""
                    )
            _prg = apply_payment_reply_guard(
                reply=reply or "",
                inbound_text=message or "",
                inbound_metadata=_prg_meta,
                payment_receipt_received=bool(
                    getattr(new_state.order_prep, "payment_receipt_received", False)
                ),
                chosen_path=_chosen_path,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            if _prg.replaced:
                reply = _prg.reply
                _guard_replaced["payment_reply_guard"] = True
        except Exception as _prg_exc:  # noqa: BLE001
            logger.warning(
                "[PAYMENT_REPLY_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _prg_exc,
            )

        try:
            from modules.ai.brain.postprocess.shipment_truth_guard import (  # noqa: PLC0415
                apply_shipment_truth_guard,
            )
            _stg = apply_shipment_truth_guard(
                reply=reply or "",
                commerce_bundle=getattr(ctx, "commerce_bundle", None),
                inbound_metadata=(profile or {}).get("inbound_metadata") or {},
                payment_receipt_received=bool(
                    getattr(new_state.order_prep, "payment_receipt_received", False)
                ),
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            if _stg.replaced:
                reply = _stg.reply
                _guard_replaced["shipment_truth_guard"] = True
                if _stg.scrubbed_empty:
                    result.data["shipment_claim_scrubbed_empty"] = True
                    result.data["shipment_guard_blocked_claims"] = list(
                        _stg.blocked_claims
                    )
        except Exception as _stg_exc:  # noqa: BLE001
            logger.warning(
                "[SHIPMENT_TRUTH_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _stg_exc,
            )

        try:
            from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: PLC0415
                apply_staff_escalation_truth_guard,
            )
            _escalation_path = _chosen_path or str(getattr(decision, "action", "") or "")
            _setg = apply_staff_escalation_truth_guard(
                reply=reply or "",
                inbound_text=message or "",
                inbound_metadata=(profile or {}).get("inbound_metadata") or {},
                chosen_path=_escalation_path,
                brain_handoff=(str(getattr(decision, "action", "") or "") == ACTION_HANDOFF),
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                state=new_state,
                history=history,
            )
            if _setg.replaced:
                reply = _setg.reply
                _guard_replaced["staff_escalation_truth_guard"] = True
            if _setg.staff_escalation_claim_blocked:
                result.data["staff_escalation_claim_blocked"] = True
                if _setg.reason:
                    result.data["staff_escalation_guard_reason"] = _setg.reason
        except Exception as _setg_exc:  # noqa: BLE001
            logger.warning(
                "[STAFF_ESCALATION_TRUTH_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _setg_exc,
            )

        try:
            from modules.ai.brain.postprocess.staff_presence_truth_guard import (  # noqa: PLC0415
                apply_staff_presence_truth_guard,
            )
            _sptg = apply_staff_presence_truth_guard(
                reply=reply or "",
                inbound_text=message or "",
                db=db,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                state=new_state,
                store_contact_phone=str(
                    (getattr(ctx.facts, "store_contact_phone", None) if ctx else None)
                    or (profile or {}).get("store_contact_phone")
                    or ""
                ),
            )
            if _sptg.replaced:
                reply = _sptg.reply
                _guard_replaced["staff_presence_truth_guard"] = True
            if _sptg.staff_presence_claim_blocked:
                result.data["staff_presence_claim_blocked"] = True
                if _sptg.reason:
                    result.data["staff_presence_guard_reason"] = _sptg.reason
        except Exception as _sptg_exc:  # noqa: BLE001
            logger.warning(
                "[STAFF_PRESENCE_TRUTH_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _sptg_exc,
            )

        _availability_ctx: Optional[Dict[str, Any]] = None

        try:
            from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: PLC0415
                apply_product_availability_truth_guard,
                product_availability_guard_mode,
            )
            if product_availability_guard_mode() != "off":
                from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: PLC0415
                    build_availability_context,
                )
                _pavg_rec_ids: list = []
                for _rec in (getattr(new_state, "last_recommended_products", None) or [])[:5]:
                    _rid = (_rec or {}).get("id") if isinstance(_rec, dict) else None
                    if isinstance(_rid, int):
                        _pavg_rec_ids.append(_rid)
                _availability_ctx = build_availability_context(
                    db,
                    tenant_id,
                    focus_product=getattr(new_state, "current_product_focus", None),
                    recommended_product_ids=_pavg_rec_ids,
                )
                _pavg = apply_product_availability_truth_guard(
                    reply=reply or "",
                    availability_context=_availability_ctx,
                    inbound_text=message or "",
                    chosen_path=_chosen_path,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                )
                if _pavg.replaced:
                    reply = _pavg.reply
                    _guard_replaced["product_availability_truth_guard"] = True
                if _pavg.availability_claim_blocked:
                    result.data["availability_claim_blocked"] = True
                    if _pavg.reason:
                        result.data["availability_guard_reason"] = _pavg.reason
        except Exception as _pavg_exc:  # noqa: BLE001
            logger.warning(
                "[PRODUCT_AVAILABILITY_TRUTH_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _pavg_exc,
            )

        try:
            from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: PLC0415
                apply_product_claim_grounding_guard,
                product_claim_grounding_guard_mode,
            )
            if product_claim_grounding_guard_mode() != "off" and not _navigator_owner_locked:
                if _availability_ctx is None:
                    from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: PLC0415
                        build_availability_context,
                    )
                    _pcgg_rec_ids: list = []
                    for _rec in (getattr(new_state, "last_recommended_products", None) or [])[:5]:
                        _rid = (_rec or {}).get("id") if isinstance(_rec, dict) else None
                        if isinstance(_rid, int):
                            _pcgg_rec_ids.append(_rid)
                    _availability_ctx = build_availability_context(
                        db,
                        tenant_id,
                        focus_product=getattr(new_state, "current_product_focus", None),
                        recommended_product_ids=_pcgg_rec_ids,
                    )
                _pcgg_meta = dict((profile or {}).get("inbound_metadata") or {})
                _pcgg_meta["inbound_text"] = message or ""
                _pcgg_meta["decision_topic"] = str((decision.args or {}).get("topic") or "")
                for _flag in (
                    "block_catalog_push",
                    "block_staff_contact",
                    "block_showroom_location",
                    "pause_order_slot_collection",
                ):
                    if _flag in (decision.args or {}):
                        _pcgg_meta[_flag] = bool((decision.args or {}).get(_flag))
                try:
                    from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: PLC0415
                        active_catalog_group_slug_from_state,
                        resolve_catalog_category_scope,
                    )
                    from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: PLC0415
                        active_category_from_state,
                        extract_browse_category_scope,
                    )

                    _cat_subject = extract_browse_category_scope(message or "", "")
                    if _cat_subject:
                        _cat_scope = resolve_catalog_category_scope(
                            db,
                            tenant_id,
                            message or "",
                            _cat_subject,
                            active_group_slug=active_catalog_group_slug_from_state(new_state),
                            active_category=active_category_from_state(new_state),
                        )
                        if _cat_scope.must_filter_by_category and not _cat_scope.specific_product:
                            _pcgg_meta["category_browse"] = True
                            _pcgg_meta["specific_product"] = False
                            _pcgg_meta["use_catalog_prices_only"] = True
                            if _cat_scope.matched_category:
                                _pcgg_meta["active_category"] = _cat_scope.matched_category
                except Exception:  # noqa: BLE001  # noqa: silent-ok — metadata enrich must not block guard
                    pass
                try:
                    from modules.ai.brain.state.price_objection_topic import (  # noqa: PLC0415
                        detect_price_objection_topic_shift,
                    )

                    if detect_price_objection_topic_shift(message or ""):
                        _pcgg_meta["price_objection"] = True
                except Exception:  # noqa: BLE001  # noqa: silent-ok — metadata enrich must not block guard
                    pass
                _pcgg = apply_product_claim_grounding_guard(
                    reply=reply or "",
                    db=db,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    availability_context=_availability_ctx,
                    executor_products=list(result.data.get("products") or []),
                    chosen_path=_chosen_path,
                    history=history,
                    order_state=new_state,
                    inbound_metadata=_pcgg_meta,
                )
                if _pcgg.replaced:
                    reply = _pcgg.reply
                    _guard_replaced["product_claim_grounding_guard"] = True
                if _pcgg.blocked_claims:
                    result.data["product_claim_blocked"] = True
                    result.data["product_claim_blocked_kinds"] = list(_pcgg.blocked_claims)
                    if _pcgg.reason:
                        result.data["product_claim_guard_reason"] = _pcgg.reason
        except Exception as _pcgg_exc:  # noqa: BLE001
            logger.warning(
                "[PRODUCT_CLAIM_GROUNDING_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _pcgg_exc,
            )

        try:
            from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: PLC0415
                apply_catalog_product_grounding_guard,
                catalog_product_grounding_guard_mode,
            )
            if catalog_product_grounding_guard_mode() != "off" and not _navigator_owner_locked:
                _cpgg_meta = dict((profile or {}).get("inbound_metadata") or {})
                _cpgg_meta["decision_topic"] = str((decision.args or {}).get("topic") or "")
                for _flag in (
                    "block_catalog_push",
                    "block_staff_contact",
                    "block_showroom_location",
                    "pause_order_slot_collection",
                ):
                    if _flag in (decision.args or {}):
                        _cpgg_meta[_flag] = bool((decision.args or {}).get(_flag))
                _cpgg_category = str((decision.args or {}).get("category_hint") or "").strip()
                if not _cpgg_category:
                    from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
                        extract_inquiry_product_query,
                        extract_types_overview_query,
                    )
                    _cpgg_category = (
                        extract_types_overview_query(message or "")
                        or extract_inquiry_product_query(message or "")
                    )
                _cpgg = apply_catalog_product_grounding_guard(
                    reply=reply or "",
                    inbound_text=message or "",
                    category_hint=_cpgg_category,
                    availability_context=_availability_ctx,
                    executor_products=list(result.data.get("products") or []),
                    chosen_path=_chosen_path,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    order_state=new_state,
                    inbound_metadata=_cpgg_meta,
                    intent=intent,
                )
                if _cpgg.replaced:
                    reply = _cpgg.reply
                    _guard_replaced["catalog_product_grounding_guard"] = True
                if _cpgg.ungrounded_mentions:
                    result.data["catalog_product_grounding_blocked"] = True
                    result.data["ungrounded_product_mentions"] = list(_cpgg.ungrounded_mentions)
                    if _cpgg.reason:
                        result.data["catalog_product_grounding_reason"] = _cpgg.reason
        except Exception as _cpgg_exc:  # noqa: BLE001
            logger.warning(
                "[CATALOG_PRODUCT_GROUNDING_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _cpgg_exc,
            )

        if _navigator_owner_locked:
            from .catalog.navigation import owner_reply_hash  # noqa: PLC0415

            _reply_changed = (reply or "") != _owned_reply_snapshot
            _path_changed = str(result.data.get("chosen_path") or "") != _owned_path_snapshot
            _kind_changed = str(result.data.get("discovery_output_kind") or "") != _owned_kind_snapshot
            if _reply_changed or _path_changed or _kind_changed:
                logger.warning(
                    "[CATALOG_NAVIGATOR] owner_replacement_blocked tenant=%s path=%s "
                    "reply_changed=%s path_changed=%s kind_changed=%s",
                    tenant_id,
                    _owned_path_snapshot,
                    _reply_changed,
                    _path_changed,
                    _kind_changed,
                )
                reply = _owned_reply_snapshot
                result.data["chosen_path"] = _owned_path_snapshot
                if _owned_kind_snapshot:
                    result.data["discovery_output_kind"] = _owned_kind_snapshot
                result.data["owner_replacement_blocked"] = True
            result.data["navigator_owner"] = True
            result.data["owner_locked"] = True
            result.data["owner_replaced"] = False
            if not result.data.get("owner_reply_hash"):
                result.data["owner_reply_hash"] = owner_reply_hash(reply or "")

        try:
            from modules.ai.brain.postprocess.commerce_tail_guard import (  # noqa: PLC0415
                apply_commerce_tail_guard,
            )

            _ctg = apply_commerce_tail_guard(
                reply=reply or "",
                ctx=ctx,
                intent_name=str(getattr(intent, "name", "") or ""),
                inbound_text=message or "",
                conversation_objective=str(
                    getattr(new_state, "active_conversation_objective", "") or ""
                ),
                chosen_path=_chosen_path,
                tenant_id=tenant_id,
            )
            if _ctg.stripped:
                reply = _ctg.reply
                _guard_replaced["commerce_tail_guard"] = True
        except Exception as _ctg_exc:  # noqa: BLE001
            logger.warning(
                "[COMMERCE_TAIL_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _ctg_exc,
            )

        try:
            from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: PLC0415
                apply_commerce_reply_quality_guard,
            )

            if not result.data.get("shipment_claim_scrubbed_empty"):
                _crqg = apply_commerce_reply_quality_guard(
                    reply=reply or "",
                    inbound_text=message or "",
                    intent_name=str(getattr(intent, "name", "") or ""),
                    primary_customer_goal=str(
                        getattr(getattr(ctx, "reply_state", None), "primary_customer_goal", "")
                        or ""
                    ),
                    conversation_objective=str(
                        getattr(state, "active_conversation_objective", "") or ""
                    ),
                    locale=str((profile or {}).get("preferred_language") or "ar"),
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    state=new_state,
                    inbound_metadata=(profile or {}).get("inbound_metadata") or {},
                    decision_topic=str((decision.args or {}).get("topic") or ""),
                    availability_polarity=str(
                        (decision.args or {}).get("availability_polarity") or ""
                    ),
                    chosen_path=_chosen_path,
                    kb_availability_facts=(decision.args or {}).get("allowed_facts"),
                )
                if _crqg.replaced:
                    reply = _crqg.reply
                    _guard_replaced["commerce_reply_quality_guard"] = True
        except Exception as _crqg_exc:  # noqa: BLE001
            logger.warning(
                "[COMMERCE_REPLY_QUALITY_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _crqg_exc,
            )

        try:
            from modules.ai.brain.postprocess.saudi_dialect_guard import (  # noqa: PLC0415
                apply_saudi_dialect_guard,
            )

            _sdg = apply_saudi_dialect_guard(
                reply or "",
                locale=str((profile or {}).get("preferred_language") or "ar"),
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            if _sdg.replaced:
                reply = _sdg.reply
                _guard_replaced["saudi_dialect_guard"] = True
        except Exception as _sdg_exc:  # noqa: BLE001
            logger.warning(
                "[SAUDI_DIALECT_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _sdg_exc,
            )

        try:
            from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
                customer_authored_caption,
            )
            from modules.ai.brain.postprocess.general_image_reply_post_guard import (  # noqa: PLC0415
                apply_general_image_reply_post_guard,
            )

            _gigr = apply_general_image_reply_post_guard(
                reply or "",
                topic=str((decision.args or {}).get("topic") or ""),
                chosen_path=_chosen_path,
                safe_image_facts=dict((decision.args or {}).get("safe_image_facts") or {}),
                customer_caption=customer_authored_caption(message or ""),
                tenant_id=tenant_id,
            )
            if _gigr.replaced:
                reply = _gigr.reply
                _guard_replaced["general_image_reply_post_guard"] = True
        except Exception as _gigr_exc:  # noqa: BLE001
            logger.warning(
                "[GENERAL_IMAGE_REPLY_POST_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _gigr_exc,
            )

        try:
            from modules.ai.brain.commerce_reply_humanizer import (  # noqa: PLC0415
                apply_commerce_reply_humanizer,
            )

            _reply_state = getattr(ctx, "reply_state", None)
            _selected = getattr(_reply_state, "selected_product", None) or {}
            _product_title = str(
                _selected.get("title") or _selected.get("name") or ""
            ).strip()
            _crh = apply_commerce_reply_humanizer(
                reply=reply or "",
                inbound_text=message or "",
                intent_name=str(getattr(intent, "name", "") or ""),
                primary_customer_goal=str(
                    getattr(_reply_state, "primary_customer_goal", "") or ""
                ),
                locale=str((profile or {}).get("preferred_language") or "ar"),
                chosen_path=_chosen_path,
                human_priority=bool(getattr(ctx, "human_priority", False)),
                product_title=_product_title,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                turn_id=getattr(new_state, "turn", None),
                post_guard_rewrite=bool(
                    _guard_replaced.get("product_availability_truth_guard")
                    or _guard_replaced.get("commerce_reply_quality_guard")
                ),
            )
            if _crh.replaced:
                reply = _crh.reply
                _guard_replaced["commerce_reply_humanizer"] = True
            result.data["humanizer_style_signature"] = _crh.style_signature
            result.data["humanizer_emoji_bucket"] = _crh.emoji_bucket
            result.data["humanizer_product_category"] = _crh.product_category
            result.data["post_guard_rewrite_applied"] = _crh.post_guard_rewrite_applied
            result.data["style_layer_applied"] = _crh.style_layer_applied
            result.data["operational_fact_detected"] = _crh.operational_fact_detected
        except Exception as _crh_exc:  # noqa: BLE001
            logger.warning(
                "[COMMERCE_REPLY_HUMANIZER] pipeline hook failed tenant=%s err=%s",
                tenant_id, _crh_exc,
            )

        try:
            from modules.ai.brain.postprocess.gender_agreement_guard import (  # noqa: PLC0415
                apply_gender_agreement_guard,
            )
            from core.customer_display import looks_like_phone_personalization_name  # noqa: PLC0415

            _gender_customer_name = ""
            if isinstance(profile, dict):
                _raw_gn = str(
                    profile.get("name") or profile.get("customer_name") or ""
                ).strip()
                if _raw_gn and not looks_like_phone_personalization_name(_raw_gn):
                    _gender_customer_name = _raw_gn

            _gag = apply_gender_agreement_guard(
                reply or "",
                message=message or "",
                customer_name=_gender_customer_name,
                state=new_state,
                profile=profile if isinstance(profile, dict) else None,
                tenant_id=tenant_id,
            )
            if _gag.replaced:
                reply = _gag.reply
                _guard_replaced["gender_agreement_guard"] = True
                result.data["gender_agreement_style"] = _gag.reply_style
                result.data["gender_agreement_source"] = _gag.gender_source
        except Exception as _gag_exc:  # noqa: BLE001
            logger.warning(
                "[GENDER_AGREEMENT_GUARD] pipeline hook failed tenant=%s err=%s",
                tenant_id, _gag_exc,
            )
            _crh = None

        try:
            from modules.ai.brain.final_reply_source import (  # noqa: PLC0415
                log_final_reply_source,
                resolve_final_source,
            )

            log_final_reply_source(
                tenant_id=tenant_id,
                intent=str(getattr(intent, "name", "") or ""),
                chosen_path=_chosen_path,
                final_source=resolve_final_source(
                    chosen_path=_chosen_path,
                    guard_replaced=_guard_replaced,
                    humanizer_changed=bool(
                        _guard_replaced.get("commerce_reply_humanizer")
                    ),
                ),
                llm_model=_model_used,
                llm_provider=str(result.data.get("llm_provider") or ""),
                truth_guard_changed=bool(
                    _guard_replaced.get("product_availability_truth_guard")
                ),
                quality_guard_changed=bool(
                    _guard_replaced.get("commerce_reply_quality_guard")
                ),
                humanizer_changed=bool(
                    _guard_replaced.get("commerce_reply_humanizer")
                ),
                post_guard_rewrite_applied=bool(
                    result.data.get("post_guard_rewrite_applied")
                ),
                product_category=str(
                    result.data.get("humanizer_product_category") or ""
                ),
                emoji_bucket=str(result.data.get("humanizer_emoji_bucket") or ""),
                style_signature=str(
                    result.data.get("humanizer_style_signature") or ""
                ),
                reply_text=reply or "",
            )
        except Exception as _frs_exc:  # noqa: BLE001
            logger.warning(
                "[FINAL_REPLY_SOURCE] pipeline hook failed tenant=%s err=%s",
                tenant_id, _frs_exc,
            )

        if _final_turn_contract is not None:
            try:
                from .turn.final_turn_audit import audit_final_turn_reply  # noqa: PLC0415

                _ftc_post_guards = audit_final_turn_reply(
                    _final_turn_contract,
                    reply or "",
                    phase="post_postprocess",
                    tenant_id=tenant_id,
                    result_data=dict(getattr(result, "data", None) or {}),
                )
                if _ftc_post_guards.has_violations:
                    result.data["final_turn_violations_post_postprocess"] = list(
                        _ftc_post_guards.violations
                    )
            except Exception as _ftc_guard_audit_exc:  # noqa: BLE001  # noqa: silent-ok — shadow audit must not block reply
                logger.debug(
                    "[FINAL_TURN_VIOLATION] post_postprocess audit skipped tenant=%s err=%s",
                    tenant_id,
                    _ftc_guard_audit_exc,
                )

        # ── Persona ownership snapshot (measurement-only) ───────────────
        try:
            from .persona_ownership import build_brain_persona_ownership  # noqa: PLC0415

            _persona_ownership = build_brain_persona_ownership(
                decision_action=str(getattr(decision, "action", "") or ""),
                decision_args=dict(getattr(decision, "args", None) or {}),
                reply_state=getattr(ctx, "reply_state", None),
                chosen_path=_chosen_path,
                guard_replaced=_guard_replaced,
            )
            _persona_ownership_dict = _persona_ownership.to_dict()
            result.data["persona_ownership"] = _persona_ownership_dict
        except Exception as _po_exc:  # noqa: BLE001
            _persona_ownership_dict = {}
            logger.exception(
                "[PERSONA_OWNERSHIP] brain snapshot failed tenant=%s",
                tenant_id,
            )

        # ── 10. Structured turn trace (searchable in Railway logs) ────────
        #
        # Single per-turn record — every field the merchant's audit
        # request asked for. Existing dashboards read ``[BrainTurn]``
        # so we extend the same line rather than introducing a
        # parallel log channel.
        try:
            logger.info(
                "[BrainTurn] %s",
                json.dumps({
                    "tenant_id":     tenant_id,
                    "phone":         customer_phone[-4:] if len(customer_phone) >= 4 else "****",
                    "turn":          new_state.turn,
                    "message_len":   len(message),
                    # Inbound preview — truncated for log volume; the
                    # full body lives in MessageEvent. The merchant's
                    # audit explicitly asked for ``last_user_message``
                    # so a misclassification can be traced without
                    # joining tables.
                    "inbound_preview": (message or "")[:160],
                    # Intent layer
                    "detected_intent": intent.name,
                    "confidence":    round(intent.confidence, 2),
                    "slots":         intent.slots,
                    "method":        intent.extraction_method,
                    "social_category": _social_category,
                    # State transition
                    "stage_before":  stage_before,
                    "stage_after":   new_state.stage,
                    "greeted":       new_state.greeted,
                    "product_focus": (new_state.current_product_focus or {}).get("title"),
                    "draft_order":   new_state.draft_order_id,
                    "order_prep_missing": list(getattr(new_state.order_prep, "missing_fields", []) or []),
                    # Order / payment state — for post-order context
                    # disambiguation (delivery vs receipt).
                    "order_status":  str(getattr(new_state.order_prep, "order_status", "") or ""),
                    "awaiting_payment_receipt": bool(
                        getattr(new_state.order_prep, "awaiting_payment_receipt", False)
                    ),
                    "payment_receipt_received": bool(
                        getattr(new_state.order_prep, "payment_receipt_received", False)
                    ),
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
                    "chosen_path":        _chosen_path,
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
                    "response_mode":    "llm" if _chosen_path.startswith("llm") else "template",
                    "fallback_used":    _fallback_used,
                    "model_used":       _model_used,
                    "reply_len":        len(reply or ""),
                    # Persona ownership (measurement-only)
                    "persona_stamped":  _persona_ownership_dict.get("persona_stamped"),
                    "persona_topic":    _persona_ownership_dict.get("persona_topic"),
                    "persona_kind":     _persona_ownership_dict.get("persona_kind"),
                    "bypass_reason":    _persona_ownership_dict.get("bypass_reason"),
                    "expression_owner": _persona_ownership_dict.get("expression_owner"),
                    "compose_pass_count": _persona_ownership_dict.get("compose_pass_count"),
                    # Final answer-alignment outcome.
                    "alignment_passed":   _align_passed,
                    "alignment_mismatch": _align_mismatch,
                    "alignment_regen":    _align_regen_fired,
                    "latency_ms":       latency_ms,
                    "pre_commerce_shortcut": bool(_pre_commerce_shortcut),
                    "intent_priority": (
                        _intent_priority.to_trace_dict()
                        if _intent_priority is not None
                        else None
                    ),
                }, ensure_ascii=False),
            )
        except Exception:
            pass   # trace logging must never break the reply path

        # ── Relational moment passthrough (Tenant 33 #49 — Commit 3) ──
        # The webhook's safety-net loop consults this token to decide
        # whether the cold-info nets (store_link / location) get a
        # chance to fire on top of an emotionally-correct reply.
        # Empty string when the relational layer is disabled or no
        # moment is set — guarantees zero behaviour change with
        # ``RELATIONAL_LAYER_ENABLED=false``.
        _relational_moment_token: str = ""
        try:
            rel_state = getattr(ctx, "relational_state", None)
            mom = getattr(rel_state, "moment", None) if rel_state is not None else None
            mom_value = getattr(mom, "value", None)
            if isinstance(mom_value, str) and mom_value and mom_value != "none":
                _relational_moment_token = mom_value
        except Exception:
            _relational_moment_token = ""

        try:
            from modules.ai.brain.observability.order_flow_evidence import (  # noqa: PLC0415
                emit_pipeline_turn_evidence,
            )

            emit_pipeline_turn_evidence(
                tenant_id=tenant_id,
                phone=customer_phone,
                turn=getattr(new_state, "turn", None),
                message=message or "",
                intent=intent,
                inbound_metadata=(profile or {}).get("inbound_metadata") or {},
                state_before=state,
                state_after=new_state,
                reply=reply or "",
                decision_action=str(getattr(decision, "action", "") or ""),
                chosen_path=_chosen_path,
                decision_reason=str(getattr(decision, "reason", "") or ""),
            )
        except Exception as _ofe_exc:  # noqa: BLE001  # noqa: silent-ok — evidence emit must not break turn
            logger.debug(
                "[ORDER_FLOW_EVIDENCE] pipeline emit skipped tenant=%s err=%s",
                tenant_id,
                _ofe_exc,
            )

        return {
            "reply": reply,
            "buttons": pending_buttons,
            "handoff": decision.action == ACTION_HANDOFF,
            "relational_moment": _relational_moment_token,
            "persona_ownership": _persona_ownership_dict,
            "non_commerce_block_mode": bool(
                getattr(ctx, "block_commerce_escalation", False)
            ),
            "non_commerce_category": str(
                getattr(ctx, "non_commerce_category", "") or ""
            ),
            "decision_action": str(getattr(decision, "action", "") or ""),
            "decision_args": dict(getattr(decision, "args", None) or {}),
            "intent": str(getattr(intent, "name", "") or ""),
            "social_human_context_category": str(
                getattr(_social_human_context, "category", "") or ""
            ),
            "native_catalog_entry": dict(result.data.get("native_catalog_entry") or {}),
            "outbound_text_policy": dict(result.data.get("outbound_text_policy") or {}),
            "shipment_claim_scrubbed_empty": bool(
                result.data.get("shipment_claim_scrubbed_empty")
            ),
            "shipment_guard_blocked_claims": list(
                result.data.get("shipment_guard_blocked_claims") or []
            ),
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
    _args = decision.args or {}
    if str(_args.get("customer_goal") or _args.get("response_goal") or "") in {
        "all_variant_prices",
        "show_all_variants_prices",
    }:
        return "all_variant_prices"
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
    args_path = str((decision.args or {}).get("chosen_path") or "").strip()
    if args_path:
        return args_path
    if decision.action == ACTION_CATALOG_NAVIGATE:
        return str((decision.args or {}).get("chosen_path") or "catalog_navigation")
    if decision.action == "llm_reply":
        return "llm"
    if decision.action in {"greet", "faq_reply", "clarify", "narrow_choices"}:
        return "rule"
    if decision.action == "payment_transfer_promise":
        return "payment_transfer_promise"
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
    db: Any = None,
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

    _conversation_summary = str(current_state.conversation_summary or "")
    if getattr(ctx, "fresh_social_context", False):
        from .context.fresh_social_context import filter_recent_turns_for_fresh_social  # noqa: PLC0415

        _conversation_summary = ""
        recent_turns = filter_recent_turns_for_fresh_social(
            current_message=ctx.message or "",
        )
        recent_customer_messages = [
            str(ctx.message or "").strip(),
        ] if str(ctx.message or "").strip() else []

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
    _browse_defocus = bool((merchant_context or {}).get("browse_defocus"))
    if not _browse_defocus:
        try:
            from .commerce.product_breadth_policy import (  # noqa: PLC0415
                global_availability_browse_requested,
            )

            _browse_defocus = global_availability_browse_requested(ctx.message or "")
        except Exception:  # noqa: BLE001
            _browse_defocus = False
    selected_product = (
        None
        if _browse_defocus
        else (current_state.current_product_focus or None)
    )

    platform_kb_mode = False
    platform_topic = ""
    platform_kb_excerpt = ""
    non_commerce_block_mode = bool(
        (decision.args or {}).get("block_commerce_escalation")
    )
    if not non_commerce_block_mode:
        try:
            from .intent.non_commerce_classifier import resolve_commerce_block  # noqa: PLC0415
            _nc = resolve_commerce_block(
                ctx.message or "",
                intent_name=getattr(ctx.intent, "name", None),
                intent_confidence=getattr(ctx.intent, "confidence", None),
            )
            non_commerce_block_mode = _nc is not None
        except Exception:  # noqa: BLE001
            pass
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
        "store_url_resolved": bool(getattr(ctx.facts, "store_url_resolved", False)),
        "store_url_source": str(getattr(ctx.facts, "store_url_source", "") or "none"),
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
    try:
        from .commerce.store_inquiry_compose_guard import (  # noqa: PLC0415
            is_store_link_compose_turn,
        )

        if is_store_link_compose_turn(
            intent_name=getattr(ctx.intent, "name", "") or "",
            decision_action=str(decision.action or ""),
            decision_topic=str((decision.args or {}).get("topic") or ""),
            customer_message=ctx.message or "",
        ):
            selected_product = None
            known_facts["checkout_preparation"] = {}
            known_facts["store_url"] = ctx.facts.store_url
            known_facts["store_url_resolved"] = bool(
                getattr(ctx.facts, "store_url_resolved", bool(ctx.facts.store_url))
            )
            known_facts["store_url_source"] = str(
                getattr(ctx.facts, "store_url_source", "") or "none"
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — store-link defocus must not block compose
        pass
    _sr = getattr(ctx, "state_relevance", None)
    if _sr is not None and hasattr(_sr, "to_dict"):
        known_facts["state_relevance_verdict"] = _sr.to_dict()

    try:
        from .state.price_objection_topic import (  # noqa: PLC0415
            build_price_objection_facts,
            detect_price_objection_topic_shift,
            enrich_price_objection_facts_with_active_order,
        )

        if detect_price_objection_topic_shift(ctx.message or ""):
            known_facts["price_objection"] = enrich_price_objection_facts_with_active_order(
                build_price_objection_facts(ctx.message or ""),
                state=current_state,
                order_prep=getattr(current_state, "order_prep", None),
                inbound_metadata=dict((ctx.profile or {}).get("inbound_metadata") or {}),
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — price objection facts must not block compose
        pass

    if str((decision.args or {}).get("topic") or "") == "kb_availability_facts":
        known_facts["kb_availability"] = dict(
            (decision.args or {}).get("allowed_facts") or {}
        )

    if str((decision.args or {}).get("topic") or "") == "image_ack_or_clarify":
        _safe_image_facts = dict((decision.args or {}).get("safe_image_facts") or {})
        if _safe_image_facts:
            known_facts["safe_image_facts"] = _safe_image_facts

    _commerce_navigator = None
    _merchant_sales_channels = None
    try:
        from .commerce.sales_channel_capabilities import (  # noqa: PLC0415
            resolve_merchant_sales_channels,
        )

        _merchant_sales_channels = resolve_merchant_sales_channels(
            None,
            int(getattr(ctx, "tenant_id", 0) or 0),
            store_url=str(ctx.facts.store_url or ""),
            store_url_source=str(getattr(ctx.facts, "store_url_source", "") or ""),
            maps_url=str(getattr(ctx.facts, "maps_url", "") or ""),
        )
        known_facts["sales_channel_availability"] = (
            _merchant_sales_channels.availability_facts()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — sales channel facts must not block compose
        pass

    try:
        from .commerce.commerce_navigator import resolve_commerce_navigator  # noqa: PLC0415

        _profile_meta = dict((ctx.profile or {}).get("inbound_metadata") or {})
        _commerce_navigator = resolve_commerce_navigator(
            message=ctx.message or "",
            intent_name=getattr(ctx.intent, "name", "") or "",
            intent_slots=dict(getattr(ctx.intent, "slots", None) or {}),
            decision_topic=str((decision.args or {}).get("topic") or ""),
            stage=str(current_state.stage or ""),
            order_prep=getattr(current_state, "order_prep", None),
            state=current_state,
            inbound_metadata=_profile_meta,
            store_url=str(ctx.facts.store_url or ""),
            maps_url=str(getattr(ctx.facts, "maps_url", "") or ""),
            whatsapp_phone=str(ctx.customer_phone or ""),
            merchant_sales_channels=_merchant_sales_channels,
        )
        known_facts["commerce_navigator"] = _commerce_navigator.to_dict()
        logger.info(
            "[COMMERCE_NAVIGATOR] tenant=%s stage=%s next_goal=%s",
            getattr(ctx, "tenant_id", None),
            _commerce_navigator.stage,
            _commerce_navigator.next_goal,
        )
    except Exception as _cn_exc:  # noqa: BLE001
        logger.debug(
            "[COMMERCE_NAVIGATOR] skipped tenant=%s err=%s",
            getattr(ctx, "tenant_id", None),
            _cn_exc,
        )

    _checkout_order_context = _load_checkout_order_context(
        db,
        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
        customer_id=getattr(ctx, "customer_id", None),
        conversation_id=getattr(ctx, "conversation_id", None),
        phone=str(ctx.customer_phone or ""),
        state=current_state,
        message=str(ctx.message or ""),
        inbound_metadata=dict((ctx.profile or {}).get("inbound_metadata") or {}),
    )
    if _checkout_order_context is not None:
        try:
            from core.order_context_prefill import build_checkout_compose_facts  # noqa: PLC0415

            known_facts["checkout_identity_shipping"] = build_checkout_compose_facts(
                _checkout_order_context,
                message=str(ctx.message or ""),
                phone=str(ctx.customer_phone or ""),
            )
        except Exception as _cif_exc:  # noqa: BLE001  # noqa: silent-ok — checkout facts must not block compose
            logger.debug(
                "[CHECKOUT_COMPOSE_FACTS] skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _cif_exc,
            )
        try:
            from .commerce.commerce_navigator import resolve_commerce_navigator  # noqa: PLC0415

            _profile_meta = dict((ctx.profile or {}).get("inbound_metadata") or {})
            _commerce_navigator = resolve_commerce_navigator(
                message=ctx.message or "",
                intent_name=getattr(ctx.intent, "name", "") or "",
                intent_slots=dict(getattr(ctx.intent, "slots", None) or {}),
                decision_topic=str((decision.args or {}).get("topic") or ""),
                stage=str(current_state.stage or ""),
                order_prep=getattr(current_state, "order_prep", None),
                state=current_state,
                inbound_metadata=_profile_meta,
                store_url=str(ctx.facts.store_url or ""),
                maps_url=str(getattr(ctx.facts, "maps_url", "") or ""),
                whatsapp_phone=str(ctx.customer_phone or ""),
                merchant_sales_channels=_merchant_sales_channels,
                order_context=_checkout_order_context,
            )
            known_facts["commerce_navigator"] = _commerce_navigator.to_dict()
        except Exception as _cn2_exc:  # noqa: BLE001  # noqa: silent-ok — navigator refresh must not block compose
            logger.debug(
                "[COMMERCE_NAVIGATOR] refresh skipped tenant=%s err=%s",
                getattr(ctx, "tenant_id", None),
                _cn2_exc,
            )

    effective_tone = tenant_tone or str(ctx.profile.get("communication_style") or "neutral")

    from .persona_expression import persona_topic_from_decision_args  # noqa: PLC0415

    try:
        from modules.ai.brain.postprocess.staff_presence_compose import (  # noqa: PLC0415
            enrich_decision_args_for_staff_presence_compose,
        )

        enrich_decision_args_for_staff_presence_compose(
            decision,
            db=db,
            tenant_id=getattr(ctx, "tenant_id", None),
            message=ctx.message or "",
            state=current_state,
            store_contact_phone=str(getattr(ctx.facts, "store_contact_phone", "") or ""),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[STAFF_PRESENCE_COMPOSE] skipped tenant=%s",
            getattr(ctx, "tenant_id", None),
        )

    _persona_topic = persona_topic_from_decision_args(decision.args)
    _persona_kind = str((decision.args or {}).get("persona_kind") or "").strip()
    _contextual_clarify = (
        decision.action == ACTION_LLM_REPLY
        and str((decision.args or {}).get("topic") or "") == "contextual_clarify"
    )
    _ambiguity_class = str((decision.args or {}).get("ambiguity_class") or "").strip()
    _clarification_evidence = dict(
        (decision.args or {}).get("clarification_evidence") or {}
    )
    _intent_priority = getattr(ctx, "intent_priority", None)
    _priority_focus = ""
    _primary_goal = ""
    if _intent_priority is not None:
        _priority_focus = str(
            getattr(_intent_priority, "recommended_focus", "") or ""
        ).strip()
        _primary_goal = str(
            getattr(_intent_priority, "primary_customer_goal", "") or ""
        ).strip()
    if _persona_topic:
        logger.info(
            "[PERSONA_EXPRESSION] tenant=%s topic=%s kind=%s "
            "a1=suppressed stance=bypass",
            getattr(ctx, "tenant_id", None),
            _persona_topic,
            _persona_kind or "-",
        )

    _last_q_asked = previous_state.last_question_asked
    _last_q_answered = previous_state.last_question_answered
    try:
        from .commerce.store_inquiry_compose_guard import (  # noqa: PLC0415
            is_store_link_compose_turn,
        )

        if is_store_link_compose_turn(
            intent_name=getattr(ctx.intent, "name", "") or "",
            decision_action=str(decision.action or ""),
            decision_topic=str((decision.args or {}).get("topic") or ""),
            customer_message=ctx.message or "",
        ):
            _last_q_asked = ""
            _last_q_answered = True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — pending-q clear must not block compose
        pass

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
        last_question_asked=_last_q_asked,
        last_question_answered=_last_q_answered,
        recommended_next_step=suggestion.suggested_next_step or current_state.recommended_next_step,
        coupon_policy={
            "has_coupons": ctx.facts.has_coupons,
            "eligible_code": ctx.facts.coupon_eligibility,
            "discount_ok_now": suggestion.discount_ok_now,
            "coupon_logic_considered": suggestion.coupon_logic_considered,
        },
        recent_turns=recent_turns,
        policy_reason=str(decision.args.get("policy_reason") or ""),
        conversation_summary=_conversation_summary,
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
            decision,
            suggestion,
            stance=_stance_result,
            intent_priority=_intent_priority,
            commerce_navigator=_commerce_navigator,
            checkout_facts=dict(known_facts.get("checkout_identity_shipping") or {}),
        ),
        merchant_context=dict(merchant_context or {}),
        platform_kb_mode=platform_kb_mode,
        platform_topic=platform_topic,
        platform_kb_excerpt=platform_kb_excerpt,
        non_commerce_block_mode=non_commerce_block_mode,
        need_based_advice_mode=str(
            (decision.args or {}).get("topic") or ""
        ) in {"need_based_product_advice", "solution_seeking_commerce"}
        or getattr(ctx.intent, "name", "") in {
            "need_based_product_advice",
            "solution_seeking_commerce",
        },
        need_category=str(
            (decision.args or {}).get("solution_axis")
            or (decision.args or {}).get("need_category")
            or (getattr(ctx.intent, "slots", None) or {}).get("solution_axis")
            or (getattr(ctx.intent, "slots", None) or {}).get("need_category")
            or ""
        ),
        persona_expression_mode=bool(_persona_topic),
        persona_topic=_persona_topic,
        persona_kind=_persona_kind,
        contextual_clarify_mode=_contextual_clarify,
        ambiguity_class=_ambiguity_class,
        clarification_evidence=_clarification_evidence,
        intent_priority_focus=_priority_focus,
        primary_customer_goal=_primary_goal,
        relational_frame=(
            _persona_topic
            if _persona_topic
            else (
                _stance_result.stance
                if _stance_result and _stance_result.stance != "unknown"
                else ""
            )
        ),
        relational_evidence=(
            ""
            if _persona_topic
            else (
                _stance_result.evidence
                if _stance_result and _stance_result.stance != "unknown"
                else ""
            )
        ),
    )


def _compose_response_goal(
    decision: Decision,
    suggestion: SuggestionSnapshot,
    *,
    stance: Any = None,
    intent_priority: Any = None,
    commerce_navigator: Any = None,
    checkout_facts: Optional[Dict[str, Any]] = None,
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
    base_goal = _compose_base_response_goal(
        decision,
        suggestion,
        intent_priority=intent_priority,
        checkout_facts=checkout_facts,
    )
    from .persona_expression import persona_topic_from_decision_args  # noqa: PLC0415

    if persona_topic_from_decision_args(decision.args):
        return _prepend_intent_priority_directive(base_goal, intent_priority)
    goal_with_stance = _prepend_stance_directive(base_goal, stance)
    goal_with_priority = _prepend_intent_priority_directive(goal_with_stance, intent_priority)
    return _prepend_commerce_navigator_directive(goal_with_priority, commerce_navigator)


def _prepend_commerce_navigator_directive(base_goal: str, commerce_navigator: Any) -> str:
    if commerce_navigator is None:
        return base_goal
    try:
        from .commerce.commerce_navigator import commerce_navigator_goal_directive  # noqa: PLC0415

        directive = commerce_navigator_goal_directive(commerce_navigator)
    except Exception:  # noqa: BLE001
        return base_goal
    if not directive:
        return base_goal
    return f"{directive} | {base_goal}"


def _prepend_intent_priority_directive(base_goal: str, intent_priority: Any) -> str:
    """Prepend goal-bound priority directive when a verdict exists."""
    if intent_priority is None:
        return base_goal
    try:
        from .intent_priority.compose_hints import intent_priority_compose_directive  # noqa: PLC0415

        directive = intent_priority_compose_directive(intent_priority)
    except Exception:  # noqa: BLE001
        directive = ""
    if not directive:
        return base_goal
    return f"{directive} | {base_goal}"


def _compose_base_response_goal(
    decision: Decision,
    suggestion: SuggestionSnapshot,
    *,
    intent_priority: Any = None,
    checkout_facts: Optional[Dict[str, Any]] = None,
) -> str:
    """Decision-action-specific goal text (no stance, no relational frame).

    Pulled into its own function so the stance enrichment can wrap it
    without re-implementing every branch."""
    _checkout = dict(checkout_facts or {})
    if _checkout.get("customer_asks_known_phone") and _checkout.get("known_phone"):
        return (
            "customer_asks_known_phone — answer honestly with known_phone from "
            "CHECKOUT_IDENTITY_SHIPPING_FACTS. Do NOT ask the customer to type "
            "their phone. Do NOT use generic recovery."
        )
    if _checkout.get("customer_asks_known_name") and _checkout.get("known_name"):
        return (
            "customer_asks_known_name — answer honestly with known_name from "
            "CHECKOUT_IDENTITY_SHIPPING_FACTS. Do NOT ask the customer to re-type "
            "their name unless name_mode=ask."
        )
    _checkout_goal = str(_checkout.get("next_goal") or "").strip()
    if _checkout_goal == "confirm_customer_order_and_shipping_details_once":
        return (
            "confirm_customer_order_and_shipping_details_once — one natural Saudi Arabic "
            "WhatsApp message summarizing known order total/line items, name, phone, city, "
            "and delivery address for confirmation. Use CHECKOUT_IDENTITY_SHIPPING_FACTS "
            "only. Include order_total when order_total_known=true. Do NOT ask for phone on "
            "WhatsApp. Do NOT ask for fields marked skip. Do NOT send separate per-field "
            "confirmations in this turn."
        )
    if _checkout_goal == "confirm_customer_and_shipping_details_once":
        return (
            "confirm_customer_and_shipping_details_once — one natural Saudi Arabic "
            "WhatsApp message summarizing known name, phone, city, and delivery "
            "address for confirmation. Use CHECKOUT_IDENTITY_SHIPPING_FACTS only. "
            "Do NOT ask for phone on WhatsApp. Do NOT ask for fields marked skip. "
            "Do NOT send separate per-field confirmations in this turn."
        )
    if _checkout_goal in {
        "collect_customer_name_only",
        "collect_city_only",
        "collect_delivery_address_only",
        "confirm_customer_name_once",
        "confirm_city_once",
        "confirm_delivery_address_once",
    }:
        return (
            f"{_checkout_goal} — ask or confirm only the missing checkout field "
            "indicated in CHECKOUT_IDENTITY_SHIPPING_FACTS. Do NOT ask phone. "
            "Do NOT ask other fields already marked skip."
        )

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

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "goal_based_commerce"
    ):
        _bundle = (decision.args or {}).get("regimen_bundle") or {}
        _goal = str(_bundle.get("goal") or (decision.args or {}).get("goal") or "")
        _items = list(_bundle.get("items") or [])
        lines = [
            "goal_based_commerce — العميل يصف هدف/حاجة (ليس SKU). "
            "استخدم التوصية المنظّمة من KB — ممنوع اختراع منتجات أو تركيبات.",
            "صِغ الرد بصيغة استشارية ناعمة: «كثير من العملاء يفضلون…» "
            "«قد يناسب…» «ضمن روتين غذائي…» — ممنوع ادعاءات علاجية أو ضمان نتائج.",
            "ممنوع: علاج، يشفي، مضمون، نتائج مؤكدة، تشخيص طبي.",
        ]
        if _goal:
            lines.append(f"الهدف المكتشف: {_goal}")
        for _ug in (_bundle.get("usage_guidance") or [])[:4]:
            lines.append(f"إرشاد استخدام: {_ug}")
        for _sc in (_bundle.get("soft_claims") or [])[:3]:
            lines.append(f"claim ناعم: {_sc}")
        for _cp in (_bundle.get("compliance") or [])[:3]:
            lines.append(f"امتثال: {_cp}")
        _resolved = [i for i in _items if i.get("resolved")]
        if _resolved:
            lines.append(
                "أرسل بطاقة كل منتج محلول باستخدام "
                + "، ".join(f'`[PRODUCT:{i.get("title")}]`' for i in _resolved[:4])
                + " — مع شرح مختصر لدور كل منتج."
            )
        _followups = list(_bundle.get("followup_questions") or [])[:2]
        if _followups:
            lines.append(
                "سؤال متابعة اختياري (واحد فقط): "
                + _followups[0]
            )
        return " | ".join(lines)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "price_objection"
    ):
        _goal = str((decision.args or {}).get("response_goal") or "").strip()
        _facts = dict((decision.args or {}).get("price_objection_facts") or {})
        lines = [
            "price_objection — العميل يعترض على السعر أو يقارن بسعر منافس/مصدر آخر. "
            "ردّي بصدق وثقة دون دفاعية؛ اذكري قيمة المنتج باختصار إن وُجدت. "
            "ممنوع سؤال الكمية أو دفع checkout ما لم يطلب العميل شراءً صريحاً الآن. "
            "ممنوع تقديم خصم أو تأكيد سعر منافس من تلقاء نفسك. "
            "إذا كان سعر الكتالوج متوفراً في الحقائق، لا تقل إنه غير متوفر."
        ]
        if _facts.get("competitor_price_claim") is not None:
            lines.append(
                f"competitor_price_claim={_facts['competitor_price_claim']}"
            )
        if _facts.get("mentioned_catalog_or_expected_price") is not None:
            lines.append(
                "mentioned_catalog_or_expected_price="
                f"{_facts['mentioned_catalog_or_expected_price']}"
            )
        if _facts.get("possible_bulk_quantity") is not None:
            lines.append(f"possible_bulk_quantity={_facts['possible_bulk_quantity']}")
        if _facts.get("must_not_ask_quantity_yet"):
            lines.append("must_not_ask_quantity_yet=true")
        if _goal:
            lines.append(_goal)
        return " | ".join(lines)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "product_media"
    ):
        _goal = str((decision.args or {}).get("response_goal") or "").strip()
        if _goal:
            return _goal
        from .commerce.product_media import compose_product_media_response_goal  # noqa: PLC0415

        return compose_product_media_response_goal(
            has_vision_evidence=bool((decision.args or {}).get("has_vision_evidence")),
            has_hint_only=not bool((decision.args or {}).get("has_vision_evidence")),
            active_order_evidence=bool(
                (decision.args or {}).get("active_order_evidence")
            ),
        )

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "kb_availability_facts"
    ):
        from .commerce.non_catalog_availability_kb_route import (  # noqa: PLC0415
            compose_kb_availability_facts_goal,
        )

        return compose_kb_availability_facts_goal(decision.args or {})

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "product_visual"
    ):
        _focus = str((decision.args or {}).get("focus_product") or "").strip()
        _query = str((decision.args or {}).get("product_query") or _focus).strip()
        lines = [
            "product_visual — العميل يطلب صورة/بطاقة المنتج (ليس توصيات عامة). "
            "أرسل بطاقة المنتج الفعلية مع الصورة والسعر والرابط — "
            "ممنوع الرد النصي فقط أو «أبشري» بدون بطاقة."
        ]
        if _query:
            lines.append(
                f"المنتج المطلوب: «{_query}» — استخدم "
                f"`[PRODUCT:{_query}]` في ردك."
            )
        lines.append(
            "ممنوع اقتراح منتجات أخرى (مثل سم النحل) إذا لم يطلبها العميل."
        )
        return " | ".join(lines)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "social_persona_ack"
    ):
        from .persona_expression import compose_social_persona_goal  # noqa: PLC0415

        _sc = str((decision.args or {}).get("social_category") or "social").strip()
        return compose_social_persona_goal(_sc)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "merchant_praise_ack"
    ):
        return (
            "merchant_praise_ack — Generate a short natural Saudi Arabic "
            "WhatsApp reply. The customer is praising the shop, the service, "
            "or the merchant personally (ما شاء الله / شغل مرتب / كلك ذوق / …). "
            "Respond like a real Saudi merchant on WhatsApp: warm reciprocal "
            "gratitude in 1–2 short lines, emotionally grounded, not poetic. "
            "Mirror their warmth (name/honorific if they used one) without "
            "turning it into prose. "
            "Do NOT pitch products, prices, or checkout. "
            "Do NOT use literary or Gulf-generic phrasing such as "
            "«دوم إحساسك» / «دمت بود» / «يسعد مساك على شعورك» / "
            "«الله يبحث عنك بحسن ظنك» / «والله الثناء منك وسام» unless "
            "the customer themselves wrote in that highly literary style."
        )

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "persona_identity"
    ):
        from .persona_expression import compose_persona_identity_goal  # noqa: PLC0415

        return compose_persona_identity_goal()

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "identity_collaboration"
    ):
        from .commerce.identity_collaboration_guard import (  # noqa: PLC0415
            compose_identity_collaboration_goal,
        )

        return compose_identity_collaboration_goal()

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "image_ack_or_clarify"
    ):
        from .commerce.general_media_reply_guard import (  # noqa: PLC0415
            compose_image_ack_or_clarify_goal,
        )

        return compose_image_ack_or_clarify_goal(
            safe_image_facts=(decision.args or {}).get("safe_image_facts") or None,
        )

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "persona_social"
    ):
        from .persona_expression import compose_persona_social_goal  # noqa: PLC0415

        _pk = str((decision.args or {}).get("persona_kind") or "social").strip()
        return compose_persona_social_goal(_pk)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "non_sales_ambiguous"
    ):
        from .persona_expression import compose_non_sales_ambiguous_goal  # noqa: PLC0415

        goal = compose_non_sales_ambiguous_goal()
        overlay = str((decision.args or {}).get("staff_presence_compose_overlay") or "").strip()
        if overlay:
            goal = f"{goal} | {overlay}"
        return goal

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "conversation_recovery"
    ):
        from .persona_expression import compose_conversation_recovery_goal  # noqa: PLC0415

        _args = decision.args or {}
        return compose_conversation_recovery_goal(
            inbound_text=str(_args.get("inbound_preview") or ""),
            last_question=str(_args.get("last_question_asked") or ""),
            last_outbound=str(_args.get("last_outbound_snippet") or ""),
            recovery_reason=str(_args.get("recovery_reason") or ""),
        )

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "tracking_link_follow_up"
    ):
        _args = decision.args or {}
        _order_ref = str(_args.get("order_reference") or "").strip()
        _order_status = str(_args.get("order_status") or "").strip()
        _tracking_available = bool(_args.get("tracking_available"))
        lines = [
            "tracking_link_follow_up — Generate a short natural Saudi Arabic "
            "WhatsApp reply. The customer is asking to receive the tracking "
            "link once their existing confirmed order ships.",
            "The order already exists — acknowledge it, state the current "
            "status (pending review / not shipped yet when applicable), and "
            "confirm the tracking link will be sent here once issued.",
            "Do NOT send store_url. Do NOT ask for city/district/phone/address. "
            "Do NOT restart checkout or say you can prepare a new order.",
            "Do NOT stay silent — always produce a helpful reply.",
        ]
        if _order_ref:
            lines.append(f"Known order reference: {_order_ref}")
        if _order_status:
            lines.append(f"Known order_status: {_order_status}")
        if _tracking_available:
            lines.append(
                "tracking_available=true — include the tracking link if present "
                "in facts; otherwise explain it is not issued yet."
            )
        else:
            lines.append("tracking_available=false — no tracking URL to send yet.")
        return " | ".join(lines)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "purchase_channel_selection"
    ):
        from .commerce.checkout_route_owner import (  # noqa: PLC0415
            compose_purchase_channel_selection_goal,
        )

        return compose_purchase_channel_selection_goal(buttons_will_render=False)

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "location_delivery"
    ):
        return (
            "location_delivery — Generate a short natural Saudi Arabic "
            "WhatsApp reply. The customer asked for the store location, "
            "branches, or directions. Answer warmly in 1–2 lines using "
            "KB branch/location context when available. "
            "Do NOT push order completion or say «نكمل إنشاء طلب». "
            "Do NOT substitute the e-commerce store URL for a physical "
            "maps pin — the wire layer injects the Google Maps URL / "
            "CTA button after compose. "
            "If reaching the branch might be difficult, briefly offer "
            "to connect them with the right staff member."
        )

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "contextual_clarify"
    ):
        from .clarification.compose_goal import compose_contextual_clarify_goal  # noqa: PLC0415

        _cls = str((decision.args or {}).get("ambiguity_class") or "").strip()
        return compose_contextual_clarify_goal(
            ambiguity_class=_cls,
            intent_priority=intent_priority,
        )

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "category_discovery"
    ):
        _hint = str((decision.args or {}).get("category_hint") or "").strip()
        _label = f"«{_hint}»" if _hint else "الفئة المطلوبة"
        _base = (
            f"category_discovery — العميل يستفسر عن {_label} في وضع اكتشاف "
            "(Disclosure Ladder — خطوة 1). "
            "اذكر أسماء/أنواع متوفرة فقط من الكتالوج المزامن أو نتائج البحث "
            "(حتى 5) **بدون أسعار** في هذه الرسالة — "
            "ممنوع اختراع أسماء منتجات من المعرفة العامة أو نصوص KB القديمة. "
            "إذا لم تظهر منتجات موثقة في السياق، قل بوضوح أنك لا تستطيع تأكيد "
            "الأنواع الآن ولا تذكر أمثلة عامة. "
            "ثم سؤال توجيه واحد (تفضيل/استخدام/جنس/حجم) — "
            "ممنوع قائمة مرقمة بأسعار أو بطاقات منتجات متعددة. "
            "ممنوع اقتراح منتجات لا علاقة لها بالفئة المطلوبة."
        )
        if str((decision.args or {}).get("inquiry_kind") or "") == "types_overview":
            return (
                f"{_base} "
                f"هذا سؤال صريح عن **أنواع/variants** ضمن {_label} — "
                "اذكر الأنواع/الخيارات المتوفرة بالاسم من الكتالوج فقط (حتى 5). "
                "ممنوع الاكتفاء بسؤال الأسعار أو الأحجام بدون listing الأنواع. "
                "بعد ذكر الأنواع فقط، سؤال توجيه واحد للأسعار/الأحجام إن لزم."
            )
        return _base

    if (
        decision.action == ACTION_LLM_REPLY
        and (decision.args or {}).get("topic") == "show_all_variants_prices"
    ):
        _product = (decision.args or {}).get("product") or {}
        _title = str(_product.get("title") or "المنتج").strip()
        return (
            f"show_all_variants_prices — العميل يريد كل الأحجام/الخيارات "
            f"والأسعار لـ «{_title}». "
            "اعرض قائمة bullet واضحة بكل الأحجام المتاحة مع سعر كل واحد. "
            "ممنوع «أي منتج تقصد؟». ممنوع dump كatalog cards متعددة. "
            "اختم بسؤال قصير: أي حجم يفضّل؟"
        )

    parts: List[str] = []
    # ── Relational preference prefix (May 2026 — Tenant 33 #49, Commit 2)
    # When the relational decision router has tagged a goal token on
    # this turn (``preferred_response_goal``) we prepend it so the
    # brain reads the relational frame BEFORE the engine reason.
    # Stays a TOKEN — never prose, never an imperative; the brain
    # owns the wording.
    _preferred_goal = str(
        (decision.args or {}).get("preferred_response_goal") or ""
    ).strip()
    if _preferred_goal:
        parts.append(f"relational_goal={_preferred_goal}")
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


# Short Gulf-style salaam acknowledgments — superseded by
# ``compose.greeting_etiquette`` for level-matched salam returns.
_WELCOME_GATE_PREFIXES: List[str] = [
    "وعليكم السلام ورحمة الله 🌹",
    "وعليكم السلام 🌷",
]


def _prepend_first_contact_salaam(reply: str, ctx: Any) -> str:
    """Return ``reply`` with a level-matched salam return prepended."""
    from modules.ai.brain.compose.greeting_etiquette import (  # noqa: PLC0415
        apply_greeting_etiquette,
        customer_message_for_etiquette,
    )

    return apply_greeting_etiquette(
        reply,
        customer_message_for_etiquette(ctx),
        getattr(ctx, "state", None),
        tenant_id=getattr(ctx, "tenant_id", None),
    )


def _load_checkout_order_context(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    conversation_id: Optional[int],
    phone: str,
    state: MerchantConversationState,
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    """Build OrderContext for active WhatsApp checkout compose — read-only."""
    if db is None or not tenant_id:
        return None
    try:
        from dataclasses import asdict, is_dataclass

        from core.order_context_builder import build_order_context

        customer = None
        conversation = None
        if customer_id:
            from models import Conversation, Customer  # noqa: PLC0415

            customer = (
                db.query(Customer)
                .filter_by(id=int(customer_id), tenant_id=int(tenant_id))
                .first()
            )
            if conversation_id:
                conversation = (
                    db.query(Conversation)
                    .filter_by(id=int(conversation_id), tenant_id=int(tenant_id))
                    .first()
                )

        prep = (
            asdict(state.order_prep)
            if is_dataclass(state.order_prep)
            else dict(getattr(state.order_prep, "__dict__", {}) or {})
        )
        return build_order_context(
            db,
            tenant_id=int(tenant_id),
            conversation=conversation,
            customer=customer,
            phone=str(phone or ""),
            brain_state={"order_prep": prep},
            inbound_metadata=dict(inbound_metadata or {}),
            message=str(message or ""),
            build_source="brain_compose",
        )
    except Exception as _oc_exc:  # noqa: BLE001  # noqa: silent-ok — order context is best-effort for compose
        logger.debug(
            "[CHECKOUT_ORDER_CONTEXT] build skipped tenant=%s err=%s",
            tenant_id,
            _oc_exc,
        )
        return None


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
