"""
services/catalog_product_orchestrator.py
────────────────────────────────────────
Decision-only runtime layer for AI-driven official WhatsApp product cards.

This module evaluates a ``product_card`` attachment and returns a structured
decision — it does NOT send messages, reorder the dispatch loop, or mutate
product resolution behaviour.

Approved architecture (Phase A skeleton — not yet wired into
``whatsapp_webhook._try_send_catalog_product``):

  three resolution paths → one ``product_card`` attachment dict → orchestrator
  → (future) catalog_sender / legacy fallback

Invariants
──────────
* Stateless — no caching, persistence, or background work.
* Tenant-scoped reads only — ``Product.tenant_id == tenant_id`` enforced.
* Send readiness uses ``evaluate_tenant_catalog_send_readiness`` — NOT the
  PR2 Graph import token checklist.
* Weak confidence blocks catalog when ``CATALOG_WEAK_CONFIDENCE_BLOCK=true``
  (default).
* Retailer-id collision ALWAYS falls back — never sends official card.
* Out-of-stock and non-active catalog products fall back to legacy —
  official catalog cards require ``is_catalog_active`` (P1-G1).

Attachment immutability (commerce runtime boundary)
───────────────────────────────────────────────────
The ``product_card`` attachment dict is a **shared contract** across brain
markers, visual enforcement, safety nets, the dispatch loop, fallback
layers, delivery guards, and future commerce skills.

This module MUST treat every *attachment* as read-only:

  * MAY derive runtime values (``retailer_id``, diagnostics, decisions).
  * MAY return normalized values on :class:`ProductCardSendDecision`.
  * MUST NOT mutate the attachment dict in place — no writes, pop, update,
    injected retailer_ids, rewritten confidence/URLs/captions/titles/ids.

Phase B wiring contract (``_try_send_catalog_product``)
───────────────────────────────────────────────────────
The orchestrator is **decision-only policy** — not a transport layer,
fallback sender, payload mutator, or delivery replacement.

  1. ``decision = evaluate_product_card_send(...)`` — read-only on *attachment*.
  2. ``log_product_card_decision(decision, ...)``.
  3. ``VARIANT_PROMPT`` → Meta catalog send stays blocked until a variant
     retailer id is picked (wrong-SKU safety). The webhook must still allow
     **legacy product presentation** (image + trusted product URL) and then
     send the existing variant-question prompt — complementary, not
     mutually exclusive. Do **not** treat VARIANT_PROMPT as
     ``catalog_card_sent``.
  4. ``decision.action != SEND_CATALOG`` (all other actions) → ``return False``
     so legacy image+CTA, CTA-only, delivery guards, and rescue paths run
     unchanged.
  5. ``SEND_CATALOG`` → call ``catalog_sender`` with ``decision.retailer_id``
     (never write retailer_id back onto *attachment*).

Text-first continuity, dispatch order, and fallback ownership stay in the
webhook — the orchestrator never sends messages.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.catalog import (
    effective_retailer_id,
    effective_variant_retailer_id,
    evaluate_tenant_catalog_send_readiness,
    is_catalog_eligible,
    is_synthetic_retailer_id,
)
from core.native_catalog_capability import (
    REASON_META_CATALOG_UNVERIFIED,
    evaluate_native_catalog_product_capability,
)

logger = logging.getLogger("nahla.catalog_orchestrator")


class ProductCardSendAction(str, Enum):
    """Closed action set — callers map these to existing send paths."""

    SEND_CATALOG = "send_catalog"
    FALLBACK_LEGACY = "fallback_legacy"
    FALLBACK_CTA_ONLY = "fallback_cta_only"
    VARIANT_PROMPT = "variant_prompt"


# Closed reason vocabulary for logs, metrics, and tests.
REASON_OK = "ok"
REASON_TENANT_MISMATCH = "tenant_mismatch"
REASON_PRODUCT_NOT_FOUND = "product_not_found"
REASON_TENANT_NOT_SEND_READY = "tenant_not_send_ready"
REASON_VARIANT_CHOICE_REQUIRED = "variant_choice_required"
REASON_WEAK_CONFIDENCE = "weak_confidence"
REASON_NON_COMMERCE_BLOCKED = "non_commerce_blocked"
REASON_NO_POSITIVE_COMMERCE = "no_positive_commerce"
REASON_RETAILER_ID_COLLISION = "retailer_id_collision"
REASON_SYNTHETIC_RETAILER_ID = "synthetic_retailer_id"
REASON_NO_RETAILER_ID = "no_retailer_id"
REASON_CATALOG_NOT_ELIGIBLE = "catalog_not_eligible"
REASON_NOT_PRODUCT_CARD = "not_product_card"


def weak_confidence_block_enabled() -> bool:
    """When True (default), ``confidence=weak`` → legacy fallback."""
    raw = (os.getenv("CATALOG_WEAK_CONFIDENCE_BLOCK", "true") or "").strip().lower()
    return raw not in {"false", "0", "off", "no", ""}


def variant_send_enabled() -> bool:
    """Mirrors ``CATALOG_VARIANT_SEND`` in ``whatsapp_webhook``."""
    raw = (os.getenv("CATALOG_VARIANT_SEND", "true") or "").strip().lower()
    return raw not in {"false", "0", "off", "no", ""}


@dataclass(frozen=True)
class ProductCardSendDecision:
    """Outcome of :func:`evaluate_product_card_send`.

    ``log_event`` is the primary grep token:
      * ``CATALOG_ORCHESTRATE`` — normal decision trail
      * ``CATALOG_RID_COLLISION`` — collision-specific (reason also set)
    """

    action: ProductCardSendAction
    reason: str
    retailer_id: str = ""
    stock_warning: bool = False
    log_event: str = "CATALOG_ORCHESTRATE"
    tenant_send_ready: bool = False
    product_ready: bool = False
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_log_dict(
        self,
        *,
        tenant_id: Optional[int],
        product_id: Optional[Any] = None,
        confidence: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Structured payload for ``[CATALOG_ORCHESTRATE]`` / collision logs."""
        return {
            "event":           self.log_event,
            "tenant_id":       tenant_id,
            "product_id":      product_id,
            "action":          self.action.value,
            "reason":          self.reason,
            "retailer_id":     self.retailer_id or None,
            "stock_warning":   self.stock_warning,
            "confidence":      confidence,
            "tenant_ready":    self.tenant_send_ready,
            "product_ready":   self.product_ready,
            **self.diagnostics,
        }


def _decision(
    action: ProductCardSendAction,
    reason: str,
    *,
    retailer_id: str = "",
    stock_warning: bool = False,
    log_event: str = "CATALOG_ORCHESTRATE",
    tenant_send_ready: bool = False,
    product_ready: bool = False,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> ProductCardSendDecision:
    return ProductCardSendDecision(
        action=action,
        reason=reason,
        retailer_id=retailer_id,
        stock_warning=stock_warning,
        log_event=log_event,
        tenant_send_ready=tenant_send_ready,
        product_ready=product_ready,
        diagnostics=diagnostics or {},
    )


def _bound_variant_from_attachment(attachment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Exact bound variant referent, or None when the parent product is bound.

    Never invents a sibling variant just to obtain a Meta-compatible id.
    A bound variant with an empty retailer id still counts as bound so
    native send fails closed instead of substituting the parent SKU.
    """
    picked = (attachment.get("picked_variant_retailer_id") or "").strip()
    vid = attachment.get("picked_variant_id") or attachment.get("selected_variant_id")
    if picked:
        return {"id": vid, "retailer_id": picked}
    if vid not in (None, ""):
        return {"id": vid, "retailer_id": ""}
    return None


def resolve_attachment_retailer_id(
    attachment: Dict[str, Any],
    product_row: Any,
) -> str:
    """Variant-aware retailer id — mirrors ``_try_send_catalog_product``."""
    picked = (attachment.get("picked_variant_retailer_id") or "").strip()
    if picked:
        return picked
    if product_row is not None:
        rid = (
            effective_variant_retailer_id(product_row)
            or effective_retailer_id(product_row)
        )
        if rid:
            return rid
    return effective_retailer_id(attachment)


def count_retailer_id_owners(
    products: List[Any],
    retailer_id: str,
) -> List[int]:
    """Return product ids in *products* sharing *retailer_id* (effective)."""
    rid = (retailer_id or "").strip()
    if not rid:
        return []
    owners: List[int] = []
    for p in products:
        eff = effective_variant_retailer_id(p) or effective_retailer_id(p)
        if eff == rid:
            pid = getattr(p, "id", None)
            if pid is None and isinstance(p, dict):
                pid = p.get("id")
            if pid is not None:
                owners.append(int(pid))
    return owners


def retailer_id_has_collision(
    products: List[Any],
    retailer_id: str,
) -> bool:
    """True when more than one product in the tenant scope shares *retailer_id*."""
    return len(count_retailer_id_owners(products, retailer_id)) > 1


def query_retailer_id_collision_peer_ids(
    db: Any,
    *,
    tenant_id: int,
    retailer_id: str,
    exclude_product_id: Optional[int] = None,
    limit: int = 2,
) -> List[int]:
    """Return up to *limit* peer product ids sharing *retailer_id* (effective).

    Scoped strictly to *tenant_id*, excludes *exclude_product_id* (the
    attachment under evaluation). One indexed query — never loads the
    full tenant catalog.
    """
    rid = (retailer_id or "").strip()
    if not rid or db is None:
        return []
    try:
        from models import Product as _Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        from database.models import Product as _Product  # noqa: PLC0415

    from sqlalchemy import and_, func, or_  # noqa: PLC0415

    meta_trim = func.trim(_Product.meta_retailer_id)
    ext_trim = func.trim(_Product.external_id)
    meta_empty = or_(
        _Product.meta_retailer_id.is_(None),
        meta_trim == "",
    )
    effective_match = or_(
        meta_trim == rid,
        and_(meta_empty, ext_trim == rid),
    )

    q = (
        db.query(_Product.id)
        .filter(_Product.tenant_id == tenant_id)
        .filter(effective_match)
    )
    if exclude_product_id is not None:
        q = q.filter(_Product.id != int(exclude_product_id))
    rows = q.limit(max(1, int(limit))).all()
    return [int(r[0] if isinstance(r, tuple) else r.id) for r in rows]


def evaluate_product_card_send(
    *,
    tenant_id: int,
    connection: Any,
    attachment: Dict[str, Any],
    product_row: Optional[Any] = None,
    collision_peer_ids: Optional[List[int]] = None,
    tenant_products: Optional[List[Any]] = None,
    block_commerce_escalation: bool = False,
    positive_commerce_intent: bool = False,
    membership: Optional[Any] = None,
) -> ProductCardSendDecision:
    """Pure decision function — no I/O, no sends, no side effects.

    Parameters
    ──────────
    tenant_id:
        JWT-scoped tenant for this turn.
    connection:
        Cached ``WhatsAppConnection`` for the tenant (may be None).
    attachment:
        Standard ``product_card`` dict from the resolver / safety nets.
    product_row:
        Optional pre-loaded ``Product`` ORM row. When omitted, collision
        checks use *tenant_products* only; tenant isolation on the row
        is skipped unless *product_row* is supplied by the caller.
    collision_peer_ids:
        Peer product ids returned by
        :func:`query_retailer_id_collision_peer_ids` — any non-empty list
        triggers collision fallback. Prefer this over *tenant_products*.
    tenant_products:
        Deprecated in-memory collision scan — retained for unit tests only.
        Production wiring MUST use *collision_peer_ids* from the scoped query.

    Phase A: callers pass pre-fetched rows. Phase B wiring may load
    ``product_row`` inside ``_try_send_catalog_product`` before invoking
    this helper — still one scoped query, no orchestrator-owned session.

    **Attachment immutability:** *attachment* is read-only input. Derived
    values (``retailer_id``, eligibility, stock flags) are returned on the
    :class:`ProductCardSendDecision` only — never written back onto the dict.
    """
    # Read-only contract — attachment must not be mutated anywhere below.
    if block_commerce_escalation:
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_NON_COMMERCE_BLOCKED,
            diagnostics={"block_commerce_escalation": True},
        )

    if not attachment or attachment.get("kind") != "product_card":
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_NOT_PRODUCT_CARD,
        )

    # ── Tenant send readiness (NOT import / Graph token checklist) ──
    tenant_ready = evaluate_tenant_catalog_send_readiness(connection)
    if not tenant_ready.ready:
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_TENANT_NOT_SEND_READY,
            tenant_send_ready=False,
            diagnostics={
                "tenant_send_reason": tenant_ready.reason,
                "tenant_checks":      tenant_ready.checks,
            },
        )

    # ── Variant choice short-circuit (Meta catalog only) ───────────
    # Product-level rich presentation (legacy image + product URL) remains
    # allowed. This action blocks binding a Meta catalog retailer_id until
    # the customer pins a sellable variant — it must not suppress the
    # product card itself.
    picked_variant_rid = (attachment.get("picked_variant_retailer_id") or "").strip()
    if (
        variant_send_enabled()
        and not picked_variant_rid
        and bool(attachment.get("needs_variant_choice"))
    ):
        return _decision(
            ProductCardSendAction.VARIANT_PROMPT,
            REASON_VARIANT_CHOICE_REQUIRED,
            tenant_send_ready=True,
            diagnostics={"variant_count": len(attachment.get("variants") or [])},
        )

    # ── Tenant isolation on loaded product row ────────────────────────
    if product_row is not None:
        row_tid = getattr(product_row, "tenant_id", None)
        if row_tid is None and isinstance(product_row, dict):
            row_tid = product_row.get("tenant_id")
        if row_tid is not None and int(row_tid) != int(tenant_id):
            return _decision(
                ProductCardSendAction.FALLBACK_LEGACY,
                REASON_TENANT_MISMATCH,
                tenant_send_ready=True,
                diagnostics={"row_tenant_id": row_tid},
            )

    # ── Weak confidence gate ────────────────────────────────────────
    confidence = (attachment.get("confidence") or "").strip().lower()
    if weak_confidence_block_enabled() and confidence == "weak":
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_WEAK_CONFIDENCE,
            tenant_send_ready=True,
            diagnostics={"confidence": confidence},
        )

    # ── Positive commerce intent gate (May 2026) ────────────────────
    # Weak / missing attachment confidence requires explicit commerce
    # intent — unless ops disabled the legacy weak-confidence block.
    if (
        not positive_commerce_intent
        and confidence in {"", "weak", "low"}
        and not (confidence == "weak" and not weak_confidence_block_enabled())
    ):
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_NO_POSITIVE_COMMERCE,
            tenant_send_ready=True,
            diagnostics={
                "confidence": confidence or "missing",
                "positive_commerce_intent": False,
            },
        )

    retailer_id = resolve_attachment_retailer_id(attachment, product_row)
    if not retailer_id:
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_NO_RETAILER_ID,
            tenant_send_ready=True,
        )

    if is_synthetic_retailer_id(retailer_id):
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_SYNTHETIC_RETAILER_ID,
            tenant_send_ready=True,
            retailer_id=retailer_id,
        )

    # ── Collision — ALWAYS fallback ───────────────────────────────────
    if collision_peer_ids:
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_RETAILER_ID_COLLISION,
            tenant_send_ready=True,
            retailer_id=retailer_id,
            log_event="CATALOG_RID_COLLISION",
            diagnostics={
                "collision_peer_ids":  list(collision_peer_ids),
                "collision_count":     len(collision_peer_ids),
            },
        )
    if tenant_products is not None and retailer_id_has_collision(
        tenant_products, retailer_id,
    ):
        owners = count_retailer_id_owners(tenant_products, retailer_id)
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_RETAILER_ID_COLLISION,
            tenant_send_ready=True,
            retailer_id=retailer_id,
            log_event="CATALOG_RID_COLLISION",
            diagnostics={
                "collision_owner_ids": owners,
                "collision_count":     len(owners),
            },
        )

    # ── Per-product catalog eligibility ─────────────────────────────
    product_target = product_row if product_row is not None else attachment
    elig = is_catalog_eligible(connection, products=[product_target])
    if not elig.ok:
        return _decision(
            ProductCardSendAction.FALLBACK_LEGACY,
            REASON_CATALOG_NOT_ELIGIBLE,
            tenant_send_ready=True,
            retailer_id=retailer_id,
            diagnostics={"eligibility_reason": elig.reason},
        )

    catalog_id = str(getattr(connection, "meta_catalog_id", "") or "").strip()
    capability = evaluate_native_catalog_product_capability(
        product_target,
        catalog_id=catalog_id,
        variant=_bound_variant_from_attachment(attachment),
        membership=membership,
        intended_retailer_id=retailer_id,
        tenant_id=tenant_id,
    )
    bound_product_id = capability.product_id
    if bound_product_id is None:
        raw_pid = attachment.get("id")
        if raw_pid is not None:
            try:
                bound_product_id = int(raw_pid)
            except (TypeError, ValueError):
                bound_product_id = raw_pid
    capability_diag = {
        "native_catalog_available": capability.available,
        "mapping_status": capability.mapping_status,
        "mapping_provenance": capability.provenance,
        "capability_reason": capability.reason,
        "canonical_product_id": bound_product_id,
        "canonical_variant_id": capability.variant_id,
    }

    # ── Stock warning (diagnostics only when catalog send proceeds) ───
    in_stock = attachment.get("in_stock")
    if in_stock is None and product_row is not None:
        in_stock = getattr(product_row, "in_stock", True)
    stock_warning = in_stock is False

    # ── CTA-only hint when no image (caller still owns send) ─────────
    has_image = bool((attachment.get("file_url") or "").strip())
    action = ProductCardSendAction.SEND_CATALOG
    if not has_image and not (attachment.get("product_url") or "").strip():
        # Nothing to render in legacy either — still send_catalog attempt
        # first; provider may succeed with catalog-only body.
        pass
    elif not has_image:
        action = ProductCardSendAction.FALLBACK_CTA_ONLY

    if not capability.available:
        capability_diag["membership_fail_closed"] = True
        if action == ProductCardSendAction.SEND_CATALOG:
            return _decision(
                ProductCardSendAction.FALLBACK_LEGACY,
                capability.reason or REASON_META_CATALOG_UNVERIFIED,
                tenant_send_ready=True,
                retailer_id=retailer_id,
                diagnostics=capability_diag,
            )

    if action == ProductCardSendAction.SEND_CATALOG:
        retailer_id = capability.retailer_id or retailer_id

    return _decision(
        action,
        REASON_OK,
        retailer_id=retailer_id,
        stock_warning=stock_warning,
        tenant_send_ready=True,
        product_ready=True,
        diagnostics={"has_image_url": has_image, **capability_diag},
    )


def should_attempt_catalog_send(decision: ProductCardSendDecision) -> bool:
    """True only when Phase B wiring should invoke ``catalog_sender``.

    All other actions (except ``VARIANT_PROMPT``, handled by the existing
    webhook branch) should ``return False`` from ``_try_send_catalog_product``
    and let legacy / CTA / delivery-guard paths proceed unchanged.
    """
    return decision.action == ProductCardSendAction.SEND_CATALOG


def catalog_send_retailer_id(decision: ProductCardSendDecision) -> str:
    """Retailer id for ``catalog_sender`` — sourced from decision, not attachment."""
    return (decision.retailer_id or "").strip()


def log_product_card_decision(
    decision: ProductCardSendDecision,
    *,
    tenant_id: Optional[int],
    attachment: Dict[str, Any],
) -> None:
    """Emit the approved structured log line — safe to call from wiring layer."""
    payload = decision.to_log_dict(
        tenant_id=tenant_id,
        product_id=attachment.get("id"),
        confidence=attachment.get("confidence"),
    )
    line = (
        f"[{decision.log_event}] tenant={tenant_id} product_id={payload.get('product_id')} "
        f"action={payload['action']} reason={payload['reason']} "
        f"retailer_id={payload.get('retailer_id')} stock_warning={payload['stock_warning']}"
    )
    if decision.reason == REASON_RETAILER_ID_COLLISION:
        logger.warning("%s owners=%s", line, payload.get("collision_owner_ids"))
    elif decision.action == ProductCardSendAction.SEND_CATALOG:
        logger.info(line)
    else:
        logger.info(line)
