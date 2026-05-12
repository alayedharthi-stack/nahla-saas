"""
campaign_wizard.recommender
───────────────────────────
Filter + score WhatsApp templates against a (goal, segment, language)
context and emit per-template badges so the wizard's Step 3 list is
short, sorted, and self-explaining.

Hard filters (template removed entirely if any fails):
  * status == APPROVED               — Meta won't accept anything else
  * is_active is not False           — merchant explicitly archived it
  * is_hidden is not True            — merchant explicitly hid it

Soft filters (template kept but down-scored or badged):
  * meta category vs goal.allowed_meta_categories
  * language matches caller-supplied lang
  * keyword/objective alignment with goal
  * recommendation_state == 'accepted'
  * segment ↔ template traits (welcome ↔ welcome objective, etc.)

Output per template:
  {
    id, name, language, category, status, components,
    score:      0–100  (clamped),
    is_best:    bool   (true for the single highest scorer),
    badges:     [str]  ("معتمد من Meta", "متوافق", "الأفضل لهذه الحملة", …)
    reason_ar:  str    (one-liner explaining why it's recommended/avoided)
  }
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models import WhatsAppTemplate

from .goals import CampaignGoal, get_goal
from .segments import CustomerSegment, get_segment

# Template library lives outside campaign_wizard but its `cohort_keys`
# field (auto-derived from the official Nahla segments registry via
# `services.crm_atoms`) is the single highest-signal indicator that a
# given template was designed for the segment the merchant just picked.
# We import lazily inside `_score_one` to avoid a circular import on
# `routers.templates` (which itself imports goal helpers).


# Score band → badge mapping. Centralised so the frontend doesn't have
# to recompute thresholds.
_BADGE_APPROVED            = "معتمد من Meta"
_BADGE_COMPATIBLE          = "متوافق"
_BADGE_NEEDS_REVIEW        = "يحتاج مراجعة"
_BADGE_BEST                = "الأفضل لهذه الحملة"
_BADGE_LANGUAGE_MISMATCH   = "لغة مختلفة"
_BADGE_CATEGORY_MISMATCH   = "فئة لا تناسب الهدف"


def _body_text(template: WhatsAppTemplate) -> str:
    """Extract BODY text from a template's components.

    Defensive against non-dict component entries (legacy Salla / 360dialog
    rows occasionally serialise components as raw strings inside
    ``WhatsAppTemplate.components``). Without the ``isinstance`` guard we
    would crash with ``'str' object has no attribute 'get'`` whenever the
    recommender hit such a row, which then bubbled up as a wizard error.
    """
    components = template.components or []
    if not isinstance(components, list):
        return ""
    for c in components:
        if not isinstance(c, dict):
            continue
        if (c.get("type") or "").upper() == "BODY":
            return c.get("text", "") or ""
    return ""


def _placeholder_count(text: str) -> int:
    """Count distinct {{N}} placeholders in a body string. We use a
    cheap regex rather than a full template parser because Meta itself
    only validates by index — anything more is overkill here."""
    import re
    return len(set(re.findall(r"\{\{(\d+)\}\}", text or "")))


def _resolve_library_meta(template: WhatsAppTemplate) -> Dict[str, Any]:
    """Return the enriched library metadata for *template* or ``{}``.

    Centralised because both ``_score_one`` (for cohort boost) and the
    public ``recommend_templates`` (for ``mode`` / ``library_label_ar``
    surfacing) need the same lookup. Imported lazily to avoid a
    circular import on ``routers.templates``.
    """
    try:
        from routers.templates import (  # noqa: PLC0415
            _resolve_library_meta_for_template,
        )
    except Exception:  # noqa: silent-ok — recommender stays useful even if templates router unavailable
        return {}
    return _resolve_library_meta_for_template(template) or {}


def _score_one(
    template: WhatsAppTemplate, goal: Optional[CampaignGoal],
    segment: Optional[CustomerSegment], lang: str,
    *,
    library_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[int, List[str], str]:
    """Return (score, badges, reason_ar) for one template. Pure function —
    does not touch the DB.

    ``library_meta`` may be passed in by the caller when the lookup has
    already been performed (avoids re-importing on every template).
    """
    badges: List[str] = []
    score = 50  # baseline for an APPROVED template that passed hard filters

    # APPROVED is a hard filter upstream, but the badge is still useful
    # to display so the merchant has the explicit confirmation.
    badges.append(_BADGE_APPROVED)

    body = _body_text(template)
    body_lower = body.lower()
    name_lower = (template.name or "").lower()
    objective = (template.objective or "").lower()

    # ── Category fit ────────────────────────────────────────────────────────
    category_ok = True
    if goal is not None and goal.allowed_meta_categories:
        if (template.category or "").upper() in goal.allowed_meta_categories:
            score += 15
        else:
            score -= 25
            category_ok = False
            badges.append(_BADGE_CATEGORY_MISMATCH)

    # ── Language fit ────────────────────────────────────────────────────────
    if lang and template.language:
        if template.language.lower() == lang.lower():
            score += 10
        else:
            score -= 5
            badges.append(_BADGE_LANGUAGE_MISMATCH)

    # ── Keyword alignment with goal ────────────────────────────────────────
    if goal is not None and goal.keywords:
        for kw in goal.keywords:
            kw_l = kw.lower()
            if kw_l in body_lower or kw_l in name_lower or kw_l in objective:
                score += 8
                break

    # ── Objective alignment with goal (more reliable than keywords) ────────
    if goal is not None and objective:
        # Heuristic mapping kept tiny; expand as the library grows.
        objective_to_goal = {
            "welcome":          "welcome",
            "abandoned_cart":   "reminder",
            "winback":          "reactivation",
            "reactivation":     "reactivation",
            "reorder":          "reorder",
            "cross_sell":       "promotion",
            "promotion":        "promotion",
            "upsell":           "promotion",
        }
        if objective_to_goal.get(objective) == goal.key:
            score += 18

    # ── Segment-aware boosts ───────────────────────────────────────────────
    #
    # Two complementary signals:
    #
    # (a) **Cohort-level intent** (highest signal). If the template
    #     library declares this template targets the merchant's chosen
    #     cohort, that's an unambiguous +20 — much more reliable than
    #     the keyword heuristics below.
    #
    # (b) **Keyword fallback** (legacy heuristic, kept). For ad-hoc
    #     templates created by the merchant outside the library we
    #     have no cohort_keys, so we still need the body/name regex
    #     boosts to surface obvious matches like "vip exclusive" /
    #     "welcome".
    if segment is not None:
        # (a) cohort-level — prefer the caller-supplied lookup when
        # available so ``recommend_templates`` doesn't pay for a
        # duplicate import per template.
        lib_meta = library_meta if library_meta is not None else _resolve_library_meta(template)
        cohort_keys = (lib_meta or {}).get("cohort_keys") or []
        if segment.key in cohort_keys:
            score += 20

        # (b) keyword fallback for non-library templates
        if segment.key == "abandoned_cart" and ("cart" in name_lower or "abandon" in objective):
            score += 15
        if segment.key == "vip" and ("vip" in name_lower or template.category == "MARKETING" and "exclusive" in body_lower):
            score += 10
        if segment.key == "new" and ("welcome" in name_lower or objective == "welcome"):
            score += 12

    # ── Merchant has previously accepted Nahla's recommendation ────────────
    if (template.recommendation_state or "").lower() == "accepted":
        score += 10

    # ── Compatibility checks ──────────────────────────────────────────────
    # Heavy templates with > 6 placeholders almost always need a manual
    # review of the variable map — flag them so the merchant doesn't
    # accidentally launch a campaign with an empty {{6}}.
    placeholder_n = _placeholder_count(body)
    if placeholder_n > 6:
        badges.append(_BADGE_NEEDS_REVIEW)
        score -= 5
    elif category_ok:
        badges.append(_BADGE_COMPATIBLE)

    score = max(0, min(score, 100))

    # ── Reason string for UI tooltip ──────────────────────────────────────
    if score >= 75 and category_ok:
        reason = "هذا القالب مناسب جداً لهدف الحملة والشريحة المختارة"
    elif score >= 55:
        reason = "قالب متوافق ويمكن استخدامه بدون مشاكل تقنية"
    elif not category_ok:
        reason = "فئة Meta لهذا القالب لا تتطابق مع نوع الحملة المختار"
    else:
        reason = "قابل للاستخدام لكن قد لا يكون الخيار الأفضل لهذا السياق"

    return score, badges, reason


def recommend_templates(
    db: Session,
    *,
    tenant_id: int,
    goal_key: Optional[str] = None,
    segment_key: Optional[str] = None,
    language: str = "ar",
) -> Dict[str, Any]:
    """Main entry point. Returns a dict ready to be JSON-serialised by
    the router:

        {
          "goal":     {...} | None,
          "segment":  {...} | None,
          "language": "ar",
          "templates": [ {... per-template ...}, ... ],
          "best_template_id": int | None,
        }
    """
    goal = get_goal(goal_key) if goal_key else None
    segment = get_segment(segment_key) if segment_key else None

    candidates = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == tenant_id,
            WhatsAppTemplate.status == "APPROVED",
            # is_active / is_hidden default to True/False on insert; we
            # still write the explicit `!= False` / `!= True` to tolerate
            # legacy NULLs from the migration window.
            (WhatsAppTemplate.is_active.is_(None)) | (WhatsAppTemplate.is_active.is_(True)),
            (WhatsAppTemplate.is_hidden.is_(None)) | (WhatsAppTemplate.is_hidden.is_(False)),
        )
        .all()
    )

    scored: List[Dict[str, Any]] = []
    for tpl in candidates:
        lib_meta = _resolve_library_meta(tpl)
        score, badges, reason = _score_one(
            tpl, goal, segment, language, library_meta=lib_meta,
        )
        # Mode / library label come from the canonical library metadata
        # so the manual vs auto distinction is identical everywhere
        # (templates page, wizard, autopilot recommender).
        mode = (lib_meta or {}).get("mode") or "auto"
        library_label_ar = (lib_meta or {}).get("library_label_ar")
        # The mode badge is rendered inline with the rest of the
        # badges so even merchants who never look at the dedicated
        # Manual/Auto pill can still see it on the card.
        mode_badge = "🟠 يدوي" if mode == "manual" else "⚡ تلقائي"
        if mode_badge not in badges:
            badges.append(mode_badge)
        scored.append({
            "id":              tpl.id,
            "name":            tpl.name,
            "language":        tpl.language,
            "category":        tpl.category,
            "status":          tpl.status,
            "components":      tpl.components or [],
            "display_name_ar": tpl.display_name_ar,
            "objective":       tpl.objective,
            "score":           score,
            "is_best":         False,  # filled below for the top scorer
            "badges":          badges,
            "reason_ar":       reason,
            # Manual vs auto contract — drives Step-3 grouping +
            # Step-4 variable inputs + Step-7 coupon section in the
            # frontend wizard.
            "mode":              mode,
            "library_label_ar":  library_label_ar,
            "auto_coupon_capable": bool((lib_meta or {}).get("auto_coupon_capable")),
        })

    scored.sort(key=lambda r: (-r["score"], r["name"] or ""))
    best_id: Optional[int] = None
    if scored and scored[0]["score"] >= 60:
        scored[0]["is_best"] = True
        if _BADGE_BEST not in scored[0]["badges"]:
            scored[0]["badges"].insert(0, _BADGE_BEST)
        best_id = scored[0]["id"]

    # ── Empty-state fallback ────────────────────────────────────────────────
    # When no APPROVED template fits, surface the closest non-approved
    # candidate (PENDING / DRAFT / REJECTED) so the merchant sees what's
    # nearly there instead of just an empty list. This drives the
    # "create / submit a template" CTA on the frontend.
    fallback: Optional[Dict[str, Any]] = None
    suggestion_ar: Optional[str] = None
    if not scored:
        near = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.status.in_(["PENDING", "DRAFT", "REJECTED"]),
                (WhatsAppTemplate.is_hidden.is_(None)) | (WhatsAppTemplate.is_hidden.is_(False)),
            )
            .all()
        )
        if near:
            # Re-use the same scoring so the language/category/keyword
            # signals still rank the closest match first.
            near_scored = []
            for tpl in near:
                s, _, _ = _score_one(tpl, goal, segment, language)
                near_scored.append((s, tpl))
            near_scored.sort(key=lambda r: -r[0])
            top_tpl = near_scored[0][1]
            fallback = {
                "id":              top_tpl.id,
                "name":            top_tpl.name,
                "language":        top_tpl.language,
                "category":        top_tpl.category,
                "status":          top_tpl.status,
                "display_name_ar": top_tpl.display_name_ar,
            }
            status = (top_tpl.status or "").upper()
            if status == "PENDING":
                suggestion_ar = (
                    f"لا يوجد قالب معتمد لهذا الهدف بعد. القالب «{top_tpl.display_name_ar or top_tpl.name}» "
                    "بانتظار موافقة Meta — تحقّق من حالته في صفحة القوالب."
                )
            elif status == "REJECTED":
                suggestion_ar = (
                    f"القالب الأقرب «{top_tpl.display_name_ar or top_tpl.name}» مرفوض من Meta — "
                    "عدّله وأعد إرساله، أو أنشئ قالباً جديداً."
                )
            else:  # DRAFT
                suggestion_ar = (
                    f"لديك مسودّة قالب «{top_tpl.display_name_ar or top_tpl.name}» — "
                    "أكملها وأرسلها لـ Meta للحصول على الاعتماد."
                )
        else:
            suggestion_ar = (
                "لا يوجد لديك أي قالب يناسب هذا الهدف. أنشئ قالباً جديداً وأرسله "
                "إلى Meta من صفحة قوالب واتساب."
            )

    return {
        "goal":             None if goal is None else {"key": goal.key, "label_ar": goal.label_ar},
        "segment":          None if segment is None else {"key": segment.key, "label_ar": segment.label_ar},
        "language":         language,
        "templates":        scored,
        "best_template_id": best_id,
        "total":            len(scored),
        # New fields for the empty-state UX. `next_best_template` is the
        # closest non-APPROVED candidate (or None); `suggestion_ar` is
        # always present when `total == 0` so the frontend can render
        # something more helpful than "no templates found".
        "next_best_template": fallback,
        "suggestion_ar":      suggestion_ar,
    }
