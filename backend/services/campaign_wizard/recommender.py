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


# Score band → badge mapping. Centralised so the frontend doesn't have
# to recompute thresholds.
_BADGE_APPROVED            = "معتمد من Meta"
_BADGE_COMPATIBLE          = "متوافق"
_BADGE_NEEDS_REVIEW        = "يحتاج مراجعة"
_BADGE_BEST                = "الأفضل لهذه الحملة"
_BADGE_LANGUAGE_MISMATCH   = "لغة مختلفة"
_BADGE_CATEGORY_MISMATCH   = "فئة لا تناسب الهدف"


def _body_text(template: WhatsAppTemplate) -> str:
    for c in (template.components or []):
        if (c.get("type") or "").upper() == "BODY":
            return c.get("text", "") or ""
    return ""


def _placeholder_count(text: str) -> int:
    """Count distinct {{N}} placeholders in a body string. We use a
    cheap regex rather than a full template parser because Meta itself
    only validates by index — anything more is overkill here."""
    import re
    return len(set(re.findall(r"\{\{(\d+)\}\}", text or "")))


def _score_one(
    template: WhatsAppTemplate, goal: Optional[CampaignGoal],
    segment: Optional[CustomerSegment], lang: str,
) -> Tuple[int, List[str], str]:
    """Return (score, badges, reason_ar) for one template. Pure function —
    does not touch the DB."""
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
    if segment is not None:
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
        score, badges, reason = _score_one(tpl, goal, segment, language)
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
        })

    scored.sort(key=lambda r: (-r["score"], r["name"] or ""))
    best_id: Optional[int] = None
    if scored and scored[0]["score"] >= 60:
        scored[0]["is_best"] = True
        if _BADGE_BEST not in scored[0]["badges"]:
            scored[0]["badges"].insert(0, _BADGE_BEST)
        best_id = scored[0]["id"]

    return {
        "goal":             None if goal is None else {"key": goal.key, "label_ar": goal.label_ar},
        "segment":          None if segment is None else {"key": segment.key, "label_ar": segment.label_ar},
        "language":         language,
        "templates":        scored,
        "best_template_id": best_id,
        "total":            len(scored),
    }
