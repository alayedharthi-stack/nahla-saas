"""
core/service_template_resolver.py
─────────────────────────────────
Service-aware template resolution layer.

KEY CONCEPTS
────────────
1. **Service** (`service_key`):  the business purpose (e.g. `cart_recovery`,
   `cod_confirmation`).  The service is the *stable identity* — templates
   are just the *current binding* that can be swapped at any time.

2. **Step** (`step_number`):  within a service a multi-step sequence may
   exist (e.g. cart recovery has steps 1-3).

3. **Active Template invariant**:  for every combination of
   `(tenant_id, service_key, step_number)` at most ONE template may be
   active (`is_active=True`) and visible (`is_hidden=False`) at any time.
   The DB enforces this with a partial unique index.

4. **Session-window rule**:
   - Inside the 24h WhatsApp service window → AI / interactive replies.
   - Outside the window → only a Meta-APPROVED template via this resolver.

PUBLIC API
──────────
  ensure_single_active(db, tenant_id, service_key, step_number, new_active_id)
      Deactivates any other template for the same slot, returns the old one.

  resolve_active_template(db, tenant_id, service_key, step_number)
      Returns the single active+visible+APPROVED template for a slot, or None.
      Strict — used by callers that must NOT auto-bind anything.

  resolve_template_for_send(db, tenant_id, service_key, step_number,
                            *, fallback_template_name=None)
      Send-flow tolerant resolver. Walks a documented fallback chain
      and AUTO-BINDS the first APPROVED template that plausibly serves
      the slot. Used by `automation_engine` so cart-recovery sends
      self-heal instead of failing with `template_not_approved` when
      the merchant's APPROVED templates exist but are unbound.

  list_alternatives(db, tenant_id, service_key, step_number)
      Returns all templates for the same slot (active first, then inactive).

  diagnose_service_slot(db, tenant_id, service_key, step_number=None)
      Returns a structured report (counts + per-template classification)
      that the dashboard / support team can use to debug why a slot
      isn't sending. Read-only; no side effects.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def ensure_single_active(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: int,
    new_active_id: int,
) -> Optional[int]:
    """
    Activate *new_active_id* and deactivate every other template that shares
    the same (tenant_id, service_key, step_number).

    Returns the id of the previously-active template (or None).
    Must be called inside an existing transaction — caller commits.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    prev_active_id: Optional[int] = None

    others = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.step_number == step_number,
            WhatsAppTemplate.is_active   == True,   # noqa: E712
            WhatsAppTemplate.id          != new_active_id,
        )
        .all()
    )
    for tpl in others:
        prev_active_id = prev_active_id or tpl.id
        tpl.is_active = False
        logger.info(
            "[ServiceResolver] Deactivated template id=%s name=%s "
            "(slot: tenant=%s service=%s step=%s) — replaced by id=%s",
            tpl.id, tpl.name, tenant_id, service_key, step_number,
            new_active_id,
        )

    target = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id        == new_active_id,
            WhatsAppTemplate.tenant_id == tenant_id,
        )
        .first()
    )
    if target:
        target.is_active = True
        target.is_hidden = False

    return prev_active_id


def resolve_active_template(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: int,
) -> Optional["WhatsAppTemplate"]:  # noqa: F821
    """
    Return the single active, visible, APPROVED template for a service slot.

    Used by the automation engine when the 24h window is CLOSED and a
    template message is the only legal send mechanism.

    Returns None when no qualifying template exists (the caller should
    log this and skip the send rather than crash).
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    return (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.step_number == step_number,
            WhatsAppTemplate.is_active   == True,   # noqa: E712
            WhatsAppTemplate.is_hidden   == False,  # noqa: E712
            WhatsAppTemplate.status      == "APPROVED",
        )
        .first()
    )


# ── Smart fallback resolution (used by the automation engine at send time) ──

# Keyword patterns the resolver uses as the FINAL safety net when no
# explicit binding, library mapping, or config name matches. Each entry
# is a service-key → list of substrings (case-folded) that strongly
# indicate the template's purpose, in EITHER Arabic or English. Order
# matters: more-specific terms come first so we prefer e.g.
# "abandoned_cart" over a generic "cart" match.
#
# This is the layer that catches merchants who created templates
# directly in Meta Business Manager with idiosyncratic names — the
# alternative is `template_not_approved` for every send.
_SERVICE_NAME_PATTERNS: Dict[str, List[str]] = {
    "cart_recovery": [
        # English — most specific first
        "abandoned_cart", "abandonedcart",
        "cart_recovery", "cartrecovery",
        "cart_reminder", "cart_followup",
        "checkout_abandoned",
        "abandoned",
        "cart",
        # Arabic
        "متروك", "متروكة",
        "السلة", "سلة",
        "تذكير_السلة", "تذكير السلة",
        "استرجاع",
    ],
    "payment_reminder": [
        # English
        "unpaid_order", "unpaid_reminder", "payment_reminder",
        "payment_pending", "pending_payment", "unpaid",
        "order_payment", "paymentreminder",
        # Arabic
        "غير_مدفوع", "غير مدفوع",
        "انتظار_الدفع", "انتظار الدفع",
        "تذكير_دفع", "تذكير الدفع",
        "دفع_معلق",
    ],
    "cod_confirmation": [
        "cod_confirmation", "cod_confirm", "cash_on_delivery",
        "تأكيد", "الدفع_عند", "عند_الاستلام",
    ],
    "order_confirmation": [
        "order_confirmation", "order_confirm", "order_placed",
        "تأكيد_الطلب", "تأكيد الطلب",
    ],
    "shipping_update": [
        "shipping", "shipment", "tracking", "delivery",
        "شحن", "تتبع", "توصيل",
    ],
    "back_in_stock": [
        "back_in_stock", "backinstock", "restock", "in_stock_again",
        "متوفر", "عاد", "عودة", "مخزون",
    ],
    "seasonal_offers": [
        "seasonal", "season", "occasion", "holiday",
        "موسم", "مناسبة", "موسمي", "عيد", "وطني",
    ],
    "salary_payday_offers": [
        "payday", "salary", "monthly_offer",
        "راتب", "رواتب", "شهري",
    ],
}


def _name_matches_service(name: Optional[str], service_key: str) -> bool:
    """Case-insensitive substring match of the template name against
    the service's keyword patterns."""
    if not name:
        return False
    n = name.lower()
    for pattern in _SERVICE_NAME_PATTERNS.get(service_key, []):
        if pattern.lower() in n:
            return True
    return False


def _library_keys_for_slot(service_key: str, step_number: int) -> List[str]:
    """Return every Nahla-library `key` that targets this service slot.

    Used to auto-bind an APPROVED template that was imported from the
    library but somehow lost its `service_key` / `step_number`
    (e.g. a `/templates/sync` after the merchant created the template
    directly in Meta Business Manager and bypassed the import flow)."""
    try:
        from services.whatsapp_templates.nahla_templates import NAHLA_TEMPLATES  # noqa: PLC0415
    except Exception:
        return []
    return [
        t["key"] for t in NAHLA_TEMPLATES
        if t.get("service_key") == service_key
        and t.get("step_number") == step_number
    ]


def _autobind(
    db: Session,
    tpl: "WhatsAppTemplate",  # noqa: F821
    *,
    tenant_id: int,
    service_key: str,
    step_number: int,
    reason: str,
) -> None:
    """Stamp `service_key` / `step_number` / `is_active=True` on a
    template that the smart resolver matched via name / source-key, so
    every subsequent send hits the strict path with zero ambiguity.

    The caller commits the surrounding transaction; this function only
    flushes so the `ensure_single_active` invariant query sees the new
    binding."""
    changed = False
    if not getattr(tpl, "service_key", None):
        tpl.service_key = service_key
        changed = True
    if getattr(tpl, "step_number", None) != step_number:
        tpl.step_number = step_number
        changed = True
    if not getattr(tpl, "is_active", False):
        tpl.is_active = True
        changed = True
    if getattr(tpl, "is_hidden", False):
        tpl.is_hidden = False
        changed = True
    if changed:
        try:
            db.flush()
            ensure_single_active(db, tenant_id, service_key, step_number, tpl.id)
            logger.info(
                "[ServiceResolver] AUTO-BIND tenant=%s service=%s step=%s "
                "tpl_id=%s name=%s reason=%s",
                tenant_id, service_key, step_number, tpl.id, tpl.name, reason,
            )
        except Exception as exc:
            logger.warning(
                "[ServiceResolver] auto-bind failed tenant=%s tpl_id=%s: %s",
                tenant_id, tpl.id, exc,
            )


def resolve_template_for_send(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: int,
    *,
    fallback_template_name: Optional[str] = None,
) -> Optional["WhatsAppTemplate"]:  # noqa: F821
    """Same intent as `resolve_active_template` but **send-flow tolerant**.

    The strict resolver has been correct for years — but in production
    we hit the failure mode where a merchant has an APPROVED template
    that is simply not bound to a service slot. Three real-world causes:

      1. Merchant created the template directly in Meta Business
         Manager. ``/templates/sync`` ingested it into a fresh row
         without `service_key` / `step_number`.
      2. A previous import row got hidden / deactivated and a new row
         arrived from sync without inheriting the binding.
      3. Sync after import: the original row's ``meta_template_id`` was
         still ``nahla_draft_*`` because the submit step crashed mid-way,
         so the sync created a parallel APPROVED row that was unbound.

    All three cases left the merchant with the right templates approved
    but the cart-recovery automation failing with
    `template_not_approved`. This resolver walks a documented chain
    that ends in **auto-binding** so the issue self-heals on the very
    next inbound cart event.

    Resolution order (first match wins):

      a. Strict: active + visible + APPROVED + matching service_key + step_number.
      b. APPROVED + matching service_key + step_number, ignoring
         is_active / is_hidden flags. Auto-promotes to active.
      c. APPROVED + matching `nahla_source_key` for one of the library
         templates that target this slot. Auto-binds & activates.
      d. APPROVED + name == ``fallback_template_name`` (the legacy
         config-level template name). Auto-binds & activates.
      e. APPROVED + matching service_key (any step_number). Auto-binds
         to the requested step; better than refusing to send.
      f. APPROVED + name matches a service-specific keyword pattern
         (e.g. "cart" / "abandoned" / "متروكة" for cart_recovery). Final
         safety net for merchants who created templates directly in Meta
         Business Manager with idiosyncratic names. Restricted to
         MARKETING / UTILITY categories so we never accidentally
         hijack an AUTHENTICATION template.

    Returns ``None`` only when the merchant truly has no APPROVED
    template that could plausibly serve the slot — at which point the
    automation engine surfaces a precise, actionable error."""
    from models import WhatsAppTemplate  # noqa: PLC0415

    log_ctx = (
        f"tenant={tenant_id} service={service_key} step={step_number} "
        f"fallback_name={fallback_template_name!r}"
    )

    # (a) strict
    tpl = resolve_active_template(db, tenant_id, service_key, step_number)
    if tpl:
        logger.info(
            "[ServiceResolver] LAYER=a (strict) HIT %s → tpl_id=%s name=%s",
            log_ctx, tpl.id, tpl.name,
        )
        return tpl
    logger.info("[ServiceResolver] LAYER=a (strict) MISS %s", log_ctx)

    # (b) drop is_active / is_hidden
    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.step_number == step_number,
            WhatsAppTemplate.status      == "APPROVED",
        )
        .order_by(WhatsAppTemplate.updated_at.desc())
        .first()
    )
    if tpl:
        logger.info(
            "[ServiceResolver] LAYER=b (inactive_match) HIT %s → tpl_id=%s name=%s",
            log_ctx, tpl.id, tpl.name,
        )
        _autobind(
            db, tpl, tenant_id=tenant_id, service_key=service_key,
            step_number=step_number, reason="strict_match_inactive",
        )
        return tpl
    logger.info("[ServiceResolver] LAYER=b (inactive_match) MISS %s", log_ctx)

    # (c) by nahla_source_key for any library template targeting this slot
    library_keys = _library_keys_for_slot(service_key, step_number)
    if library_keys:
        tpl = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id        == tenant_id,
                WhatsAppTemplate.nahla_source_key.in_(library_keys),
                WhatsAppTemplate.status           == "APPROVED",
            )
            .order_by(WhatsAppTemplate.updated_at.desc())
            .first()
        )
        if tpl:
            logger.info(
                "[ServiceResolver] LAYER=c (nahla_source_key) HIT %s "
                "library_keys=%s → tpl_id=%s name=%s source_key=%s",
                log_ctx, library_keys, tpl.id, tpl.name, tpl.nahla_source_key,
            )
            _autobind(
                db, tpl, tenant_id=tenant_id, service_key=service_key,
                step_number=step_number,
                reason=f"nahla_source_key={tpl.nahla_source_key}",
            )
            return tpl
        logger.info(
            "[ServiceResolver] LAYER=c (nahla_source_key) MISS %s library_keys=%s",
            log_ctx, library_keys,
        )
    else:
        logger.info(
            "[ServiceResolver] LAYER=c (nahla_source_key) SKIP %s — no library entries for this slot",
            log_ctx,
        )

    # (d) by config-level template_name (legacy automation seed path)
    if fallback_template_name:
        tpl = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.name      == fallback_template_name,
                WhatsAppTemplate.status    == "APPROVED",
            )
            .order_by(WhatsAppTemplate.updated_at.desc())
            .first()
        )
        if tpl:
            logger.info(
                "[ServiceResolver] LAYER=d (config_name) HIT %s → tpl_id=%s name=%s",
                log_ctx, tpl.id, tpl.name,
            )
            _autobind(
                db, tpl, tenant_id=tenant_id, service_key=service_key,
                step_number=step_number,
                reason=f"config_template_name={fallback_template_name}",
            )
            return tpl
        logger.info("[ServiceResolver] LAYER=d (config_name) MISS %s", log_ctx)

    # (e) any APPROVED template on the same service_key (any step).
    # SKIP for cart_recovery — each stage MUST use its own template,
    # never borrow from another stage.
    if service_key == "cart_recovery":
        logger.info(
            "[ServiceResolver] LAYER=e SKIP %s — cart_recovery does not "
            "allow cross-step template fallback",
            log_ctx,
        )
    else:
        tpl = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id   == tenant_id,
                WhatsAppTemplate.service_key == service_key,
                WhatsAppTemplate.status      == "APPROVED",
            )
            .order_by(
                WhatsAppTemplate.is_active.desc(),
                WhatsAppTemplate.updated_at.desc(),
            )
            .first()
        )
        if tpl:
            logger.info(
                "[ServiceResolver] LAYER=e (any_step_same_service) HIT %s → tpl_id=%s name=%s was_step=%s",
                log_ctx, tpl.id, tpl.name, tpl.step_number,
            )
            _autobind(
                db, tpl, tenant_id=tenant_id, service_key=service_key,
                step_number=step_number,
                reason=f"service_key_any_step (was step={tpl.step_number})",
            )
            return tpl
        logger.info("[ServiceResolver] LAYER=e (any_step_same_service) MISS %s", log_ctx)

    # (f) FINAL safety net: keyword pattern match on template name.
    # SKIP for cart_recovery — if layers a-d didn't find a match,
    # the stage simply doesn't send (no guessing).
    if service_key == "cart_recovery":
        logger.warning(
            "[ServiceResolver] ALL LAYERS MISS %s — "
            "No approved template found for cart_recovery step %s. "
            "The stage will NOT send.",
            log_ctx, step_number,
        )
        return None

    patterns = _SERVICE_NAME_PATTERNS.get(service_key, [])
    if patterns:
        candidates = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.status    == "APPROVED",
                WhatsAppTemplate.category.in_(["MARKETING", "UTILITY"]),
            )
            .order_by(WhatsAppTemplate.updated_at.desc())
            .all()
        )
        for cand in candidates:
            if _name_matches_service(cand.name, service_key):
                logger.info(
                    "[ServiceResolver] LAYER=f (keyword_pattern) HIT %s → "
                    "tpl_id=%s name=%s category=%s",
                    log_ctx, cand.id, cand.name, cand.category,
                )
                _autobind(
                    db, cand, tenant_id=tenant_id, service_key=service_key,
                    step_number=step_number,
                    reason=f"keyword_pattern (name={cand.name!r})",
                )
                return cand
        # Final miss — log a count of approved-but-uncategorised templates
        # so the prod log is enough to diagnose remotely.
        approved_count = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.status    == "APPROVED",
            )
            .count()
        )
        approved_names = [
            t.name for t in
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.status    == "APPROVED",
            )
            .limit(20)
            .all()
        ]
        logger.warning(
            "[ServiceResolver] ALL LAYERS MISS %s — tenant has %d APPROVED "
            "template(s); none match. names=%s",
            log_ctx, approved_count, approved_names,
        )

    return None


def list_alternatives(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: Optional[int] = None,
) -> List["WhatsAppTemplate"]:  # noqa: F821
    """
    Return all templates for a given service slot, active first.

    Useful for the frontend to show alternatives the merchant can swap in.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    q = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.is_hidden   == False,  # noqa: E712
        )
    )
    if step_number is not None:
        q = q.filter(WhatsAppTemplate.step_number == step_number)

    return q.order_by(
        WhatsAppTemplate.is_active.desc(),
        WhatsAppTemplate.updated_at.desc(),
    ).all()


# ── Diagnostic report (read-only) ────────────────────────────────────────────

def diagnose_service_slot(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: Optional[int] = None,
) -> Dict[str, Any]:
    """Read-only inspection of which template (if any) the resolver would
    pick for ``(service_key, step_number)`` and WHY every other approved
    template was rejected.

    Returned shape:
        {
            "tenant_id":     int,
            "service_key":   str,
            "step_number":   int|None,
            "would_resolve": {"id": ..., "name": ..., "via": "layer"} | None,
            "approved_total": int,
            "candidates":    [
                {
                    "id":          int,
                    "name":        str,
                    "category":    str,
                    "service_key": str|None,
                    "step_number": int|None,
                    "is_active":   bool,
                    "is_hidden":   bool,
                    "nahla_source_key": str|None,
                    "classification":  "strict_match" | "would_autobind_*" | "no_match",
                    "match_reason":    str,
                },
                ...
            ],
            "library_keys_for_slot": [str, ...],
            "name_patterns":         [str, ...],
            "recommendation":        str,
        }

    Used by the new ``GET /templates/diagnose-service/...`` endpoint and
    by support engineers when a tenant reports cart-recovery failures."""
    from models import WhatsAppTemplate  # noqa: PLC0415

    library_keys: List[str] = []
    if step_number is not None:
        library_keys = _library_keys_for_slot(service_key, step_number)
    patterns = list(_SERVICE_NAME_PATTERNS.get(service_key, []))

    approved_q = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.tenant_id == tenant_id,
        WhatsAppTemplate.status    == "APPROVED",
    )
    approved_total = approved_q.count()
    approved_rows = approved_q.order_by(
        WhatsAppTemplate.is_active.desc(),
        WhatsAppTemplate.updated_at.desc(),
    ).all()

    candidates: List[Dict[str, Any]] = []
    for t in approved_rows:
        classification = "no_match"
        reason = ""
        if (
            t.service_key == service_key
            and (step_number is None or t.step_number == step_number)
            and t.is_active
            and not t.is_hidden
        ):
            classification = "strict_match"
            reason = "active+visible+APPROVED+matching slot"
        elif t.service_key == service_key and (step_number is None or t.step_number == step_number):
            classification = "would_autobind_inactive"
            reason = "matches slot but is_active=False or is_hidden=True"
        elif t.nahla_source_key and t.nahla_source_key in library_keys:
            classification = "would_autobind_via_library"
            reason = f"nahla_source_key={t.nahla_source_key!r} matches library entry for this slot"
        elif t.service_key == service_key:
            classification = "would_autobind_other_step"
            reason = f"same service_key but step_number={t.step_number}"
        elif t.category in {"MARKETING", "UTILITY"} and _name_matches_service(t.name, service_key):
            classification = "would_autobind_keyword"
            reason = "name matches a service keyword pattern"

        candidates.append({
            "id":               t.id,
            "name":             t.name,
            "category":         t.category,
            "service_key":      t.service_key,
            "step_number":      t.step_number,
            "is_active":        bool(t.is_active),
            "is_hidden":        bool(t.is_hidden),
            "nahla_source_key": t.nahla_source_key,
            "classification":   classification,
            "match_reason":     reason,
        })

    # First non-"no_match" wins
    would_resolve: Optional[Dict[str, Any]] = None
    layer_rank = {
        "strict_match":              "a",
        "would_autobind_inactive":   "b",
        "would_autobind_via_library": "c",
        "would_autobind_other_step": "e",
        "would_autobind_keyword":    "f",
    }
    for c in candidates:
        if c["classification"] in layer_rank:
            would_resolve = {
                "id":   c["id"],
                "name": c["name"],
                "via":  layer_rank[c["classification"]],
                "classification": c["classification"],
            }
            break

    if would_resolve:
        recommendation = (
            f"الـ resolver سيختار القالب «{would_resolve['name']}» (id={would_resolve['id']}) "
            f"عبر الطبقة {would_resolve['via']}. "
            f"إذا لم يصل الإرسال، تحقق من اتصال WhatsApp وحدود Meta."
        )
    elif approved_total == 0:
        recommendation = (
            "لا يوجد أي قالب معتمد في هذا الحساب. استورد قالباً من مكتبة نحلة "
            "أو أنشئ واحداً من صفحة القوالب وقدّمه للاعتماد."
        )
    else:
        recommendation = (
            f"يوجد {approved_total} قالب معتمد لكن لا واحد منها مرتبط بخدمة "
            f"«{service_key}» ولا يطابق أنماط الأسماء المتعارف عليها. "
            f"افتح صفحة القوالب، اختر القالب المناسب، واربطه يدوياً بالخدمة "
            f"والمرحلة من زر «تعيين كنشط»."
        )

    return {
        "tenant_id":             tenant_id,
        "service_key":           service_key,
        "step_number":           step_number,
        "would_resolve":         would_resolve,
        "approved_total":        approved_total,
        "candidates":            candidates,
        "library_keys_for_slot": library_keys,
        "name_patterns":         patterns,
        "recommendation":        recommendation,
    }
